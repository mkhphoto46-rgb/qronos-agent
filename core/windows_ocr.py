"""
Reading text off a picture with Windows' own recogniser, as a hint.

Free in the two senses that matter — no tokens and no graphics memory — and it
runs on the CPU in about a quarter of a second.

**Whether it earns its place was measured, and the answer changed.** On single
dialogs the model is exact and OCR is not, so a first measurement said no. But
Qronos is not shown a dialog filling the frame; it is shown a whole 4K screen
shrunk to a 1280-pixel long edge, where the same text is a third of the size.
Measured on a generated desktop of four panels at that density:

    model alone   0.171 character error   0 of 3 codes read correctly
    OCR alone     0.096                   2 of 3, in scrambled order
    both          0.009                   2 of 3

Alone, the model reads the layout perfectly and invents the numbers — it turned
``0x8024402C`` into ``0x862480C`` and ``1,482 of 3,907 files`` into ``1.402 of
5.900 MB``. Alone, OCR reads the characters and destroys the order, interleaving
lines from four different windows. They fail in opposite directions, which is
exactly what makes one useful to the other.

So it is a **hint and never a replacement**. The picture is sent regardless, so
a reading that finds nothing costs nothing. It is labelled as a hint in the
prompt, and labelled as possibly mis-ordered, so the model reconciles it
against what it can see rather than copying it.

**There is no Persian recogniser**, and this is not a missing language pack —
Microsoft's list has no Arabic script in it at all. Shown Persian, the engine
does not fail; it produces confident nonsense like ``I O _Lå IO_JiI an_Ä Qi``.
Passing that on as a hint would be offering the model noise and calling it
evidence, so it is dropped. See :func:`useful_hint`.

**Nothing is written to disk.** The picture goes to PowerShell as base64 on
standard input and is decoded into an in-memory stream there. That keeps the
promise ``core/screen_capture.py`` makes — a capture is held in memory,
encoded, used and dropped — which a temporary file would quietly break.

PowerShell rather than a Python WinRT binding, because the binding packages are
Windows-only wheels and adding one would stop the project's dependency list
installing on the Linux half of CI, for a capability that does not exist there.
"""

from __future__ import annotations

import base64
import json
import subprocess
import sys
from dataclasses import dataclass


#: The engine refuses anything larger outright rather than scaling it.
MAX_DIMENSION = 10_000

#: Below this many words a reading is not worth passing on. A handful of
#: fragments from a mostly-graphical screen is noise with the shape of
#: evidence.
USEFUL_WORD_COUNT = 4

_SCRIPT = r"""
$ErrorActionPreference = 'Stop'

# Without this the reply comes back in the console codepage and any character
# outside it arrives as a byte Python cannot decode. Found by pointing this at
# Persian: the whole call raised, which is the one thing a hint must never do.
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8
$OutputEncoding = [System.Text.Encoding]::UTF8

Add-Type -AssemblyName System.Runtime.WindowsRuntime

$asTask = ([System.WindowsRuntimeSystemExtensions].GetMethods() |
    Where-Object {
        $_.Name -eq 'AsTask' -and
        $_.GetParameters().Count -eq 1 -and
        $_.GetParameters()[0].ParameterType.Name -eq 'IAsyncOperation`1'
    })[0]

function Await($operation, $type) {
    $task = $asTask.MakeGenericMethod($type).Invoke($null, @($operation))
    $task.Wait(-1) | Out-Null
    return $task.Result
}

# Each WinRT type has to be projected into PowerShell before it can be named,
# including the ones only mentioned as generic arguments below.
$null = [Windows.Media.Ocr.OcrEngine, Windows.Foundation, ContentType = WindowsRuntime]
$null = [Windows.Media.Ocr.OcrResult, Windows.Foundation, ContentType = WindowsRuntime]
$null = [Windows.Globalization.Language, Windows.Foundation, ContentType = WindowsRuntime]
$null = [Windows.Graphics.Imaging.BitmapDecoder, Windows.Foundation, ContentType = WindowsRuntime]
$null = [Windows.Graphics.Imaging.SoftwareBitmap, Windows.Foundation, ContentType = WindowsRuntime]
$null = [Windows.Storage.Streams.InMemoryRandomAccessStream, Windows.Foundation, ContentType = WindowsRuntime]
$null = [Windows.Storage.Streams.DataWriter, Windows.Foundation, ContentType = WindowsRuntime]

$engine = [Windows.Media.Ocr.OcrEngine]::TryCreateFromLanguage(
    [Windows.Globalization.Language]::new('en-US'))

if ($null -eq $engine) {
    Write-Output '{"ok": false, "reason": "no en-US recogniser on this machine"}'
    exit 0
}

# The picture arrives as base64 on standard input, so that a capture which
# must never become a file does not become one here.
$bytes = [System.Convert]::FromBase64String([Console]::In.ReadToEnd())

$stream = [Windows.Storage.Streams.InMemoryRandomAccessStream]::new()
$writer = [Windows.Storage.Streams.DataWriter]::new($stream)
$writer.WriteBytes($bytes)
Await ($writer.StoreAsync()) ([uint32]) | Out-Null
$writer.DetachStream() | Out-Null
$stream.Seek(0) | Out-Null

$decoder = Await ([Windows.Graphics.Imaging.BitmapDecoder]::CreateAsync($stream)) `
    ([Windows.Graphics.Imaging.BitmapDecoder])
$bitmap = Await ($decoder.GetSoftwareBitmapAsync()) `
    ([Windows.Graphics.Imaging.SoftwareBitmap])

$result = Await ($engine.RecognizeAsync($bitmap)) ([Windows.Media.Ocr.OcrResult])

$lines = @()

foreach ($line in $result.Lines) {
    $words = @()

    foreach ($word in $line.Words) {
        $words += [ordered]@{
            text = $word.Text
            x = [int]$word.BoundingRect.X
            y = [int]$word.BoundingRect.Y
            w = [int]$word.BoundingRect.Width
            h = [int]$word.BoundingRect.Height
        }
    }

    $lines += [ordered]@{ text = $line.Text; words = $words }
}

$stream.Dispose()

[ordered]@{ ok = $true; lines = $lines } | ConvertTo-Json -Depth 6 -Compress
"""


@dataclass(frozen=True)
class OcrWord:
    """One word, and where on the picture it was."""

    text: str
    x: int
    y: int
    width: int
    height: int


@dataclass(frozen=True)
class OcrReading:
    """What Windows read, or why it read nothing."""

    ok: bool
    lines: tuple[str, ...] = ()
    words: tuple[OcrWord, ...] = ()
    reason: str = ""

    @property
    def text(self) -> str:
        return "\n".join(self.lines)

    def describe(self) -> str:
        if not self.ok:
            return f"Windows read nothing: {self.reason}"

        return f"Windows read {len(self.words)} words in {len(self.lines)} lines"


def available() -> bool:
    """True when this machine has a recogniser at all. Reads nothing."""
    return sys.platform == "win32"


def word_likeness(text: str) -> float:
    """
    What fraction of this looks like actual words rather than debris.

    One, for text made of letters. Near zero, for what the engine produces when
    shown a script it has no recogniser for. Measured on real output:

        "System message The file was saved. OK"    1.00
        "I O _La IO_JiI an_A Qi"                   0.17

    Deliberately crude. It is not trying to judge whether text is correct, only
    whether it is text — and the two cases it has to separate are far apart.

    It does throw away one thing worth having. A Persian dialog with an English
    error code in it reads as ``oxC0000225 .39_4``, which scores zero and is
    dropped even though the code in it is nearly right. That is an acceptable
    loss: the model reads codes embedded in Persian correctly on its own — six
    of six, measured — and the alternative is passing it a line of invented
    Latin and calling it evidence.
    """
    tokens = [token.strip(".,:;!?()[]{}'\"-") for token in text.split()]
    tokens = [token for token in tokens if token]

    if not tokens:
        return 0.0

    wordlike = sum(
        1
        for token in tokens
        if len(token) >= 2 and token.isalpha()
    )

    return wordlike / len(tokens)


#: Below this, a reading is debris rather than text. The two cases it separates
#: measured 1.00 and 0.17, so the exact value is not load-bearing.
MINIMUM_WORD_LIKENESS = 0.45

#: How much of a reading is passed on.
#:
#: A hint is text and text costs context. A full 4K desktop reads as about
#: 3,900 characters, or a thousand tokens — most of a picture again — and the
#: declared context is 4,096. Truncated rather than refused, because "there is
#: too much on your screen" is not an answer anybody wants to a question about
#: their screen, and a partial hint is still a hint.
#:
#: 5,000 characters leaves room for the picture, the question and the answer
#: with the measured numbers: about 1,040 tokens of picture, 1,250 of hint and
#: 768 kept back for words.
MAX_HINT_CHARS = 5_000



def useful_hint(reading: OcrReading) -> str:
    """
    The reading, if it is worth passing on, and an empty string if not.

    Three ways it is not.

    It failed, in which case there is nothing to pass on and nothing is lost:
    the picture goes to the model regardless.

    It found almost nothing. A handful of fragments off a mostly-graphical
    screen is noise with the shape of evidence.

    Or it is the engine's rendering of a script it cannot read. This is the
    important one. Shown Persian, Windows does not fail — it emits confident
    Latin gibberish, which would be handed to the model as a fact about a
    picture the model can read perfectly well itself. The engine cannot emit
    Persian characters at all, so checking for them would never fire; what
    gives it away is that the output is not made of words. See
    :func:`word_likeness`.
    """
    if not reading.ok:
        return ""

    if len(reading.words) < USEFUL_WORD_COUNT:
        return ""

    if word_likeness(reading.text) < MINIMUM_WORD_LIKENESS:
        return ""

    return _clipped(reading.text)


def _clipped(text: str) -> str:
    """
    At most :data:`MAX_HINT_CHARS`, cut at a line rather than mid-word.

    Cutting mid-word would hand the model half a code, which is worse than
    handing it none: half a code looks like a whole one.
    """
    if len(text) <= MAX_HINT_CHARS:
        return text

    cut = text.rfind("\n", 0, MAX_HINT_CHARS)

    return text[: cut if cut > 0 else MAX_HINT_CHARS]


def read(png: bytes, timeout: float = 20.0) -> OcrReading:
    """
    Everything Windows can read in that picture.

    Takes the encoded bytes, not a path, and never raises for an ordinary
    failure. OCR is a hint, and a hint that throws is worse than a hint that is
    missing: the picture goes to the model regardless, so nothing is lost by
    having none.
    """
    if not available():
        return OcrReading(ok=False, reason="Windows OCR needs Windows.")

    if not png:
        return OcrReading(ok=False, reason="There is no picture to read.")

    try:
        finished = subprocess.run(
            [
                "powershell.exe",
                "-NoProfile",
                "-NonInteractive",
                "-Command",
                _SCRIPT,
            ],
            input=base64.b64encode(png).decode("ascii"),
            capture_output=True,
            text=True,
            timeout=timeout,
            encoding="utf-8",
            # Replace rather than raise. A byte that will not decode is better
            # read as one bad character than as an exception thrown through the
            # middle of a request somebody is waiting on.
            errors="replace",
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        return OcrReading(ok=False, reason=str(error))

    if finished.returncode != 0:
        return OcrReading(
            ok=False,
            reason=(finished.stderr or "PowerShell failed").strip()[:300],
        )

    return _parse((finished.stdout or "").strip())


def _parse(output: str) -> OcrReading:
    try:
        payload = json.loads(output or "{}")
    except json.JSONDecodeError:
        return OcrReading(ok=False, reason=f"Unreadable reply: {output[:200]}")

    if not isinstance(payload, dict) or not payload.get("ok"):
        reason = "unknown"

        if isinstance(payload, dict):
            reason = str(payload.get("reason", "unknown"))

        return OcrReading(ok=False, reason=reason)

    lines: list[str] = []
    words: list[OcrWord] = []

    for line in _as_list(payload.get("lines")):
        lines.append(str(line.get("text", "")))

        for word in _as_list(line.get("words")):
            words.append(
                OcrWord(
                    text=str(word.get("text", "")),
                    x=int(word.get("x", 0)),
                    y=int(word.get("y", 0)),
                    width=int(word.get("w", 0)),
                    height=int(word.get("h", 0)),
                )
            )

    return OcrReading(ok=True, lines=tuple(lines), words=tuple(words))


def _as_list(value) -> list[dict]:
    """
    PowerShell's JSON unwraps a one-element array into a bare object.

    So a screen with exactly one line of text on it comes back shaped
    differently from every other screen, which is the kind of thing that works
    for months and then does not.
    """
    if value is None:
        return []

    if isinstance(value, dict):
        return [value]

    return [item for item in value if isinstance(item, dict)]
