"""
The chat's code boundary: requests and answers checked deterministically around the Brains.

The installed runtime policy (docs/qronos_voice_output.md, "Security and ownership
boundaries"; security/permissions.py) is:

    read-only code analysis is permitted          CODE_ANALYSIS     AUTO_ALLOW
    code generation is forbidden                  CODE_GENERATION   FORBIDDEN
    code modification is forbidden                CODE_MODIFICATION FORBIDDEN
    arbitrary script execution is forbidden       SCRIPT_EXECUTION  FORBIDDEN

This module does not decide that policy; it only says which category a chat
request or a generated answer falls into. The decision, and its typed audit
record, come from ``security.gate.evaluate`` - the same gate every action uses.

Two layers, both pattern-based and model-free:

    INPUT   a request that asks Qronos to write, change or run code is refused
            before any Brain is asked (no model call, no resource admission).
            Asking what a piece of code does, or to explain or review it, is
            analysis and passes.

    OUTPUT  a generated answer that contains code - a fenced block, two or more
            consecutive code-like lines, or one shell command that changes the
            machine - is replaced by a refusal before it is recorded, shown or
            spoken. An answer that explains code in words passes.

These are deterministic patterns, not a semantic classifier: a request can be
phrased in a way the input patterns do not list, which is why the output layer
exists. Neither layer executes anything; the runtime has no code-execution
capability in its chat path. Internal model tasks that must return data (for
example the media labels and split proposals, which use their own
``answer_chat_only`` calls) are not passed through here.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Callable

from security.permissions import ActionCategory

MAX_CHECKED_CHARS = 20_000

INPUT_REFUSAL_FA = (
    "کرونوس کد نمی‌نویسد، تغییر نمی‌دهد و اجرا نمی‌کند. اگر کدی دارید، می‌توانم فقط توضیح بدهم که "
    "چه کار می‌کند یا آن را بررسی کنم."
)
OUTPUT_REFUSAL_FA = (
    "پاسخ آماده‌شده کد داشت، پس نمایش داده و خوانده نشد؛ کرونوس کد نمی‌نویسد. اگر بخواهید، همین موضوع را "
    "با کلمات توضیح می‌دهم."
)


@dataclass(frozen=True)
class CodeFinding:
    """What a request asks for, or what an answer contains; ``rule`` names the pattern (never the text)."""

    stage: str  # "input" | "output"
    category: ActionCategory
    rule: str

    @property
    def message(self) -> str:
        return INPUT_REFUSAL_FA if self.stage == "input" else OUTPUT_REFUSAL_FA


# ------------------------------------------------------------------ input (the person's request)

_LANG = (r"(?:python|javascript|typescript|java|kotlin|c\#|c\+\+|csharp|golang|rust|ruby|php|perl|powershell|"
         r"bash|zsh|shell|batch|vba|vbscript|sql|html|css|lua|matlab|autohotkey|node\.?js)")
_NOUN = (r"(?:code(?!\s+of\s+conduct)|scripts?|snippets?|macros?|functions?|methods?|regex(?:es)?|"
         r"regular\s+expressions?|one-liners?|source\s+code)")
_LANG_NOUN = r"(?:program|programs|command|commands|query|queries|app|application|class|loop)"
_FILLER = (r"(?:me|us|a|an|the|some|this|that|these|those|my|our|your|small|simple|short|quick|full|complete|"
           r"working|new|basic|little|following|above|below|" + _LANG + r")")
_GEN_VERB = r"(?:write|create|generate|make|build|produce|draft|implement|give\s+me|show\s+me|need|want)"
_MOD_VERB = (r"(?:fix|refactor|rewrite|re-write|modify|change|edit|optimi[sz]e|update|convert|port|translate|patch|"
             r"debug|complete|finish|extend|improve)")
_EXEC_VERB = r"(?:run|execute|eval|exec)"


def _verb_object(verb: str, nouns: str) -> re.Pattern[str]:
    return re.compile(
        rf"\b{verb}\b(?:\s+{_FILLER}){{0,4}}\s+(?:(?:{_LANG}\s+(?:{nouns}|{_LANG_NOUN}))|{nouns})\b",
        re.IGNORECASE)


_EN_RULES: tuple[tuple[str, ActionCategory, re.Pattern[str]], ...] = (
    ("en_generate", ActionCategory.CODE_GENERATION, _verb_object(_GEN_VERB, _NOUN)),
    ("en_code_verb", ActionCategory.CODE_GENERATION,
     re.compile(r"\b(?:can|could|would|will|please)\s+you\s+code\b|\bcode\s+(?:me|up)\b", re.IGNORECASE)),
    ("en_how_to_code", ActionCategory.CODE_GENERATION,
     re.compile(rf"\bhow\s+(?:do|can|would|should)\s+(?:i|you|we)\s+(?:write|code|implement|program)\b"
                rf"[^.?!\n]{{0,80}}\b(?:in|using|with)\s+{_LANG}\b", re.IGNORECASE)),
    ("en_modify", ActionCategory.CODE_MODIFICATION, _verb_object(_MOD_VERB, _NOUN)),
    ("en_modify_in", ActionCategory.CODE_MODIFICATION,
     re.compile(rf"\b{_MOD_VERB}\b[^.?!\n]{{0,40}}\b(?:in|of)\s+(?:my|this|the|that|our)\s+(?:{_LANG}\s+)?"
                r"(?:code|script|function|program|macro)\b", re.IGNORECASE)),
    ("en_execute", ActionCategory.SCRIPT_EXECUTION, _verb_object(_EXEC_VERB, _NOUN)),
    ("en_execute_file", ActionCategory.SCRIPT_EXECUTION,
     re.compile(r"\b(?:run|execute)\b[^\n]{0,60}\.(?:py|ps1|psm1|bat|cmd|sh|js|vbs|ahk)\b", re.IGNORECASE)),
)

_FA_LANG = (r"(?:پایتون|جاوا\s?اسکریپت|جاوا|تایپ\s?اسکریپت|سی‌شارپ|سی\s?شارپ|سی\s?پلاس\s?پلاس|پاورشل|"
            r"پاور\s?شل|بش|پی\s?اچ\s?پی|اس\s?کیو\s?ال|python|javascript|powershell|bash|sql)")
# «کد» is also a postal/national/discount/verification code: those are not program code.
_FA_CODE = (r"(?:کد(?!\s*(?:پستی|ملی|تخفیف|رهگیری|پیگیری|اقتصادی|شناسایی|تأیید|تایید|فعال‌سازی|فعالسازی|"
            r"امنیتی|ورود|پیامکی|معرف|رنگ|کالا|شعبه|سفارش|مالیاتی|بانکی|شهر|کشور))|کدی|کدش|اسکریپت|ماکرو|"
            # «برنامه» (also a plan/schedule) and «تابع»/«دستور» count only with a programming language;
            # a language name alone («یک شعر دربارهٔ پایتون بنویس») is a topic, not code.
            r"(?:برنامه|برنامه‌ای|تابع|تابعی|دستور|دستورات|فرمان|کوئری)\s+(?:به\s+زبان\s+)?" + _FA_LANG + r")")
_FA_GEN = (r"(?:بنویس|بنویسی|بنویسید|بنویسش|بساز|بسازی|بسازید|تولید\s?کن|ایجاد\s?کن|پیاده[‌\s]?سازی\s?کن|"
           r"کد\s?بزن|نوشتن)")
_FA_MOD = (r"(?:اصلاح\s?کن|درست\s?کن|درستش\s?کن|بازنویسی\s?کن|تغییر\s?بده|ویرایش\s?کن|بهینه\s?کن|تبدیل\s?کن|"
           r"ریفکتور|دیباگ\s?کن|باگش\s?را\s?بگیر|کامل\s?کن|کاملش\s?کن)")
_FA_EXEC = r"(?:اجرا\s?کن|اجراش\s?کن|اجرا\s?بکن|ران\s?کن)"


def _fa_pair(verbs: str) -> re.Pattern[str]:
    # Persian puts the object first ("یک اسکریپت پایتون بنویس که ..."); the clause must not end between them.
    return re.compile(rf"{_FA_CODE}[^.!؟?\n]{{0,60}}?{verbs}|{verbs}[^.!؟?\n]{{0,25}}?{_FA_CODE}")


_FA_RULES: tuple[tuple[str, ActionCategory, re.Pattern[str]], ...] = (
    ("fa_generate", ActionCategory.CODE_GENERATION, _fa_pair(_FA_GEN)),
    ("fa_modify", ActionCategory.CODE_MODIFICATION, _fa_pair(_FA_MOD)),
    ("fa_execute", ActionCategory.SCRIPT_EXECUTION, _fa_pair(_FA_EXEC)),
)

# A request that carries code (pasted) and TELLS Qronos to do something with it other than understanding it.
# English verbs count only as an instruction (sentence start, or after please/can you ...): "what does this
# change?" is a question about the code, "change it to ..." is a request to modify it. Persian forms are imperative.
_ASK = r"(?:^|[.!?:;\n]\s*|\b(?:please|pls|can\s+you|could\s+you|would\s+you|will\s+you|now|then|and)\s+)"
_WITH_CODE_RULES: tuple[tuple[str, ActionCategory, re.Pattern[str]], ...] = (
    ("pasted_execute", ActionCategory.SCRIPT_EXECUTION,
     re.compile(rf"{_ASK}(?:run|execute|eval)\b|اجرا\s?کن|اجراش\s?کن|ران\s?کن", re.IGNORECASE | re.MULTILINE)),
    ("pasted_generate", ActionCategory.CODE_GENERATION,
     re.compile(rf"{_ASK}(?:write|implement|extend|finish|generate)\b|بنویس|کامل\s?کن|اضافه\s?کن|ادامه\s?بده",
                re.IGNORECASE | re.MULTILINE)),
    ("pasted_modify", ActionCategory.CODE_MODIFICATION,
     re.compile(rf"{_ASK}{_MOD_VERB}\b|{_FA_MOD}", re.IGNORECASE | re.MULTILINE)),
)


def check_request(text: object) -> CodeFinding | None:
    """The forbidden category a chat request asks for, or None (analysis and everything else pass)."""
    if not isinstance(text, str) or not text.strip():
        return None
    value = text[:MAX_CHECKED_CHARS]
    if contains_code(value):
        prose = _strip_code(value)
        for rule, category, pattern in _WITH_CODE_RULES:
            if pattern.search(prose):
                return CodeFinding("input", category, rule)
    else:
        prose = value
    for rule, category, pattern in (*_EN_RULES, *_FA_RULES):
        if pattern.search(prose):
            return CodeFinding("input", category, rule)
    return None


# ------------------------------------------------------------------ output (a generated answer)

_FENCE = re.compile(r"^[ \t]*(?:```|~~~)", re.MULTILINE)
_CODE_LINE = re.compile(
    r"^(?:"
    r"(?:async\s+)?def\s+\w+\s*\(.*\)\s*(?:->.*)?:\s*$|class\s+\w+(?:\(.*\))?\s*:\s*$|"
    r"import\s+[\w.]+(?:\s+as\s+\w+)?\s*;?\s*$|from\s+[\w.]+\s+import\s+[\w*, ]+\s*$|"
    # "let x = 2" alone is also how arithmetic is written: only with a statement's semicolon.
    r"(?:export\s+)?(?:async\s+)?function\s*\w*\s*\(.*\)\s*\{?\s*$|(?:const|var)\s+\w+\s*=\s*.+$|let\s+\w+\s*=.+;\s*$|"
    r"#include\s*[<\"]|(?:public|private|protected|static)\s+[\w<>\[\],\s]+\s+\w+\s*\(.*\)\s*\{?\s*$|"
    r"(?:console\.log|System\.out\.println|printf|print|echo|puts|Write-Host|Write-Output)\s*\(.*\)\s*;?\s*$|"
    r"\$[A-Za-z_]\w*\s*=\s*[^$\n]+$|"  # a PowerShell/PHP assignment; "$x = 2$" (inline math) is not one
    r"(?:Get|Set|New|Remove|Invoke|Start|Stop|Add|Copy|Move|Rename)-[A-Z][A-Za-z]+\b.*$|"
    r"(?:SELECT\s.+\sFROM\s|INSERT\s+INTO\s|UPDATE\s+\w+\s+SET\s|DELETE\s+FROM\s|CREATE\s+(?:TABLE|INDEX)\s|"
    r"DROP\s+(?:TABLE|DATABASE)\s).*$|"
    r"<\?php|<script\b.*$|@echo\s+off\s*$|#!/.*$|"
    r"(?:if|for|while|switch|catch)\s*\(.*\)\s*\{?\s*$|"
    r"for\s+\w+(?:\s*,\s*\w+)*\s+in\s+[^:\n]+:\s*$|"
    r"(?:if|elif|while)\s+[^:\n]*(?:==|!=|<=|>=|\bnot\b|\bin\b|\bis\b|\bTrue\b|\bFalse\b)[^:\n]*:\s*$|"
    r"(?:else|try|finally)\s*:\s*$|\}\s*(?:else\b.*)?\{?\s*$|\};?\s*$|return\b.*;\s*$|"
    r"[A-Za-z_][\w.\[\]]*\s*(?:=|\+=|-=)\s*[^=].*;\s*$|[A-Za-z_][\w.]*\(.*\)\s*;\s*$|"
    # a lowercase return / a call standing alone on its line (os.listdir('.'), main()): code, not a sentence
    r"return\b.*$|[A-Za-z_]\w*(?:\.[A-Za-z_]\w*)+\(.*\)\s*$|[a-z_]\w*\([^()]*\)\s*$"
    r")")
# What a command acts on: a path-like target (quote, $var, ~, *, drive, ./ or a slash) or an ordinary word naming the
# thing ("pip install requests", "rm -rf node_modules and reinstall"). A word that explains the command instead ("rm
# -rf is dangerous", "Remove-Item is the cmdlet that deletes items", "rm -rf deletes everything") is not a target:
# that sentence is an explanation in words, not a command to run.
_NOT_A_TARGET = (r"(?:is|are|was|were|be|been|being|can|cannot|could|will|would|should|may|might|must|does|do|did|"
                 r"has|have|had|deletes?|removes?|erases?|wipes?|means?|works?|command|commands|flag|flags|option|"
                 r"options|switch|and|or|but|nor|to|on|in|of|for|from|with|without|by|at|as|into|the|a|an|this|that|"
                 r"these|those|it|its|if|when|unless|only|because|since|so|then|than|here|there|carefully|again|"
                 r"first|now|alone|itself|anywhere|everything|nothing)\b")
_TARGET = (r"(?:-\w+\s+)*(?:[\"'$~*]|[A-Za-z]:|\.{0,2}[\\/]|\S*[\\/]|"
           rf"(?!{_NOT_A_TARGET})[\w.+-]*[A-Za-z_][\w.+-]*)")
# One command that changes the machine, with what it acts on.
_COMMAND = (
    r"sudo\s+(?:-\S+\s+)*(?:rm|apt|apt-get|dnf|yum|pacman|chmod|chown|mv|cp|dd|mkfs|systemctl|sh|bash|tee|pip3?|npm)\b|"
    rf"rm\s+-[a-z]*[rf][a-z]*\s+{_TARGET}|del\s+(?:/[a-z]+\s+)+{_TARGET}|(?:rmdir|rd)\s+/s\b|format\s+[a-z]:|"
    rf"Remove-Item\s+{_TARGET}|Set-ExecutionPolicy\s+{_TARGET}|reg\s+(?:add|delete|import)\s+{_TARGET}|"
    r"bcdedit\s+/(?:set|delete|deletevalue|import)\b|vssadmin\s+(?:delete|resize)\b|wmic\s+\S+\s+(?:call|delete|set)\b|"
    r"schtasks\s+/create\b|netsh\s+(?:advfirewall|firewall|interface|wlan|winsock)\b|"
    # anything fetched and piped into an interpreter (any PowerShell Invoke-* cmdlet, iwr/irm, curl, wget)
    r"(?:iwr|irm|curl|wget|Invoke-[A-Z][A-Za-z]+)\b[^\n`]*\|\s*(?:sudo\s+(?:-\S+\s+)*)?"
    r"(?:iex|Invoke-Expression|sh|bash)\b|"
    r"(?:iex|Invoke-Expression)\s*[(\$'\"]|powershell(?:\.exe)?\s+-(?:enc|e|c|command)\b|cmd(?:\.exe)?\s+/c\b|"
    rf"chmod\s+\+x\s+{_TARGET}|pip3?\s+install\s+{_TARGET}|npm\s+(?:install|i)\s+{_TARGET}|"
    rf"choco\s+install\s+{_TARGET}|winget\s+install\s+{_TARGET}"
)
# ... where an answer hands it over to be run: at a line start (also after a list marker or a prompt sign), inside an
# inline `code span`, after a colon ("Run this: ...") or at the start of a sentence, or right after a word that hands
# it over in the middle of a clause ("delete it with rm -rf old", "then run pip install x").
_HAND_OVER = r"\b(?:run|running|use|type|typing|execute|enter|try|with|via|then|and)\s+"
_HIGH_RISK_COMMAND = re.compile(rf"(?:^[ \t>*\-$#]*|`\s*|:\s*|[.!?؟؛]\s+|{_HAND_OVER})(?:{_COMMAND})",
                                re.IGNORECASE | re.MULTILINE)
# System administration tools where an answer hands them over: at a line start (also after a list marker or a prompt
# sign) or opening an inline `code span`.
# - sudo <command>: sudo is case-sensitive where it exists, so only "sudo" as typed ("Sudo is a tool ..." is prose);
# - any netsh, wmic, bcdedit or vssadmin invocation in ANY letter case (Windows runs "NETSH INT IP RESET" or "Wmic
#   process ... delete" exactly as typed, and 78ed5dc's line-start rule matched them case-insensitively). A sentence
#   that explains the tool is told apart by the word after the name ("Netsh can ...", "WMIC is ...", "Bcdedit edits
#   ..."), never by its letter case; a bare name in a span ("the `netsh` tool") has no argument and is a mention.
_EXPLAINS_TOOL = (r"(?:lets|allows|helps|shows|displays|configures|manages|controls|provides|reports|edits|changes|"
                  r"sets|lists|offers|supports|needs|requires|runs|belongs)\b")
_ADMIN_ARGUMENT = rf"\s+(?!{_NOT_A_TARGET}|{_EXPLAINS_TOOL})[^\s`]"
_ADMIN_AT_START = re.compile(
    r"(?:^[ \t>*\-$#]*(?:\d{1,3}[.)]\s+)?|`\s*)"
    r"(?:sudo\s+(?:-\S+\s+)*[a-z][\w.-]*|"
    rf"(?i:(?:netsh|wmic)(?:\.exe)?{_ADMIN_ARGUMENT}|(?:bcdedit|vssadmin)(?:\.exe)?(?:{_ADMIN_ARGUMENT}|\s*$)))",
    re.MULTILINE)
# An inline `code span` inside a sentence holding an executable statement ("use `print('x')` here",
# "add `import os; os.remove(p)`"). A bare name or an empty call ("the `print` function", "`print()`") is a mention.
_CODE_SPAN = re.compile(r"`([^`\n]{1,400})`")
_EMPTY_CALL = re.compile(r"^[A-Za-z_][\w.]*\(\s*\)\s*;?$")
# One generated statement standing alone on its line is code even without a second line: an import, a call, a
# definition header, a declaration, a "x; y" chain, a preprocessor/script header or an SQL statement. Line kinds that
# prose also uses ("return to ...", "else:", "if it is cold:", "Get-Together") stay two-line-only (contains_code).
_STATEMENT = re.compile(
    r"^(?:"
    r"import\s+[\w.]+(?:\s+as\s+\w+)?\s*;?\s*$|from\s+[\w.]+\s+import\s+[\w*, ]+\s*$|"
    r"(?:async\s+)?def\s+\w+\s*\(.*\)\s*(?:->.*)?:.*$|class\s+\w+(?:\(.*\))?\s*:\s*$|"
    r"(?:print|printf|console\.log|System\.out\.println|echo|puts|Write-Host|Write-Output)\s*\(.*\)\s*;?\s*$|"
    r"[A-Za-z_]\w*(?:\.[A-Za-z_]\w*)+\(.*\)\s*;?\s*$|[a-z_]\w*\([^()]*\)\s*;?\s*$|"
    r"(?:const|var)\s+\w+\s*=\s*.+$|let\s+\w+\s*=.+;\s*$|\$[A-Za-z_]\w*\s*=\s*[^$\n]+$|"
    r"[A-Za-z_][\w.\[\]]*\s*(?:=|\+=|-=)\s*[^=].*;\s*$|"
    r"#include\s*[<\"]|<\?php|<script\b|@echo\s+off\s*$|#!/|"
    r"(?:SELECT\s.+\sFROM\s|INSERT\s+INTO\s|UPDATE\s+\w+\s+SET\s|DELETE\s+FROM\s|CREATE\s+(?:TABLE|INDEX)\s|"
    r"DROP\s+(?:TABLE|DATABASE)\s)|"
    r"(?:import\s+[\w.]+|from\s+[\w.]+\s+import\s+[\w*, ]+|[A-Za-z_][\w.]*\(.*?\)|"
    r"[A-Za-z_][\w.\[\]]*\s*=\s*[^;]*[(\"'][^;]*)\s*;\s*\S.*$"
    r")")
_LIST_MARKER = re.compile(r"^(?:[-*•]|\d{1,3}[.)])\s+")


def _lines(text: str) -> list[str]:
    return [line.strip().lstrip(">").strip() for line in text.splitlines() if line.strip()]


def _strip_code(text: str) -> str:
    """The request without its fenced blocks and code-like lines (what the person wrote around the code)."""
    without_fences = re.sub(r"(?:```|~~~).*?(?:```|~~~|\Z)", " ", text, flags=re.DOTALL)
    return "\n".join(line for line in without_fences.splitlines() if not _CODE_LINE.match(line.strip()))


def contains_code(text: str) -> bool:
    """A fenced block, or two consecutive code-like lines (non-empty lines, indentation ignored)."""
    if _FENCE.search(text):
        return True
    previous = False
    for line in _lines(text):
        current = bool(_CODE_LINE.match(line))
        if current and previous:
            return True
        previous = current
    return False


def check_answer(text: object) -> CodeFinding | None:
    """Whether a generated chat answer contains code that must not be shown or spoken."""
    if not isinstance(text, str) or not text.strip():
        return None
    value = text[:MAX_CHECKED_CHARS]
    if _FENCE.search(value):
        return CodeFinding("output", ActionCategory.CODE_GENERATION, "fenced_block")
    if contains_code(value):
        return CodeFinding("output", ActionCategory.CODE_GENERATION, "code_lines")
    if any(_STATEMENT.match(_LIST_MARKER.sub("", line)) for line in _lines(value)):
        return CodeFinding("output", ActionCategory.CODE_GENERATION, "single_code_line")
    if any(_STATEMENT.match(span.strip()) and not _EMPTY_CALL.match(span.strip())
           for span in _CODE_SPAN.findall(value)):
        return CodeFinding("output", ActionCategory.CODE_GENERATION, "inline_code_statement")
    if _HIGH_RISK_COMMAND.search(value) or _ADMIN_AT_START.search(value):
        return CodeFinding("output", ActionCategory.SCRIPT_EXECUTION, "machine_changing_command")
    return None


# ------------------------------------------------------------------ the decision (the existing gate)

def refused(finding: CodeFinding, *, channel: str, chars: int, audit: Callable[[Any], None] | None) -> bool:
    """Ask the action gate about ``finding``; True when it is refused (the gate records it in ``audit``).

    The audit record names the channel, the stage and the rule, and the length of the text: never the text.
    """
    from core.actions import ActionRequest
    from security import gate

    request = ActionRequest(
        category=finding.category,
        target=f"{channel} chat",
        summary=f"{finding.stage} refused by the code policy ({finding.rule}; {int(chars)} characters, text not kept)",
        parameters={"channel": channel, "stage": finding.stage, "rule": finding.rule},
    )
    try:
        verdict = gate.evaluate(request, audit=audit)
    except Exception:  # noqa: BLE001 - an unanswerable gate never lets code through
        return True
    return not verdict.allowed
