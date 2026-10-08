"""
What a linked phone can actually ask for.

Handlers are the application on top of the link. The link decides whether an
operation is permitted; a handler does it. Keeping them apart is what lets the
whole authorisation layer be tested without a model, a network or a disk.

Everything here is injected. An operation with no implementation wired in
returns ``not_configured`` rather than a stub answer, because a phone being told
"searching" by something that cannot search is worse than being told it is not
available.

``ask`` is deliberately unimplemented by default. Qronos has no Persian
text-to-speech and no wired-up model, so there is nothing honest for it to
return yet, and pretending otherwise would hide the gap.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any, Callable, Protocol

from core.config import CONFIG
from core.link_capability import Capability, LinkOp, LinkScope, resolve_capabilities
from core.link_protocol import Request
from core.link_server import Handler, LinkOperationError, LinkSession


class StatusProvider(Protocol):
    """Anything that can describe how the machine is doing."""

    def __call__(self) -> dict[str, Any]:  # pragma: no cover - protocol
        ...


class SearchFunction(Protocol):
    """
    Runs one search and returns something JSON-serialisable.

    ``emit`` is passed through so the search can report progress while it runs.
    """

    def __call__(
        self,
        query: str,
        emit: Callable[..., None],
    ) -> dict[str, Any]:  # pragma: no cover - protocol
        ...


def _require_text(request: Request, field: str, limit: int = 2_000) -> str:
    """
    Pull one string out of a request, refusing anything unusable.

    The length cap is here rather than in the protocol layer because it is a
    property of this parameter, not of frames in general.
    """

    value = request.params.get(field)

    if not isinstance(value, str) or not value.strip():
        raise LinkOperationError(
            "bad_request", f"'{field}' is required and must be text."
        )

    if len(value) > limit:
        raise LinkOperationError(
            "bad_request", f"'{field}' is longer than {limit} characters."
        )

    return value.strip()


def ping_handler(session: LinkSession, request: Request) -> dict[str, Any]:
    """
    Keepalive, and the one operation that needs no capability.

    Reports what the session may do, so the phone can grey out what it cannot
    ask for rather than discovering it by being refused.
    """

    capabilities = resolve_capabilities(session.scope, session.grants)

    return {
        "name": CONFIG.name,
        "device": session.device.device_id,
        "scope": session.scope.value,
        "capabilities": sorted(item.value for item in capabilities),
        "at": round(time.time(), 3),
    }


def remote_ping_handler(
    relay: Any,
    clock: Callable[[], float] = time.time,
) -> Handler:
    """
    ``ping``, plus what an opted-in phone needs to reach the PC from outside.

    ``relay`` is the configured ``RelayEndpoint``. Its description goes only to
    a device that is inside a remote opt-in, and only over the local network:
    that is an authenticated session with a phone in the house, so the relay
    address never has to be typed on the phone or carried in the QR code.
    Nothing in it is secret — the route is derived from the pairing key the
    phone already holds — but there is no reason to tell a phone that may not
    use it, or to repeat it to a phone that is already outside.
    """

    def handle(session: LinkSession, request: Request) -> dict[str, Any]:
        result = ping_handler(session, request)
        now = clock()

        if (
            relay is not None
            and session.scope is LinkScope.LOCAL_NETWORK
            and session.device.remote_allowed(now)
        ):
            result["remote"] = {
                "relay": relay.describe_for_phone(),
                "expires_at": session.device.remote_expires_at,
            }

        return result

    return handle


def status_handler(provider: StatusProvider | None) -> Handler:
    """Report resource and storage state, if something can supply it."""

    def handle(session: LinkSession, request: Request) -> dict[str, Any]:
        if provider is None:
            # A minimal, truthful answer: the link is up, and nothing more is
            # wired in.
            return {"link": "ok", "detail": "no status provider configured"}

        try:
            return dict(provider())
        except Exception as exc:  # pragma: no cover - provider dependent
            raise LinkOperationError(
                "status_unavailable", "Could not read the machine's status."
            ) from exc

    return handle

def search_handler(search: SearchFunction | None) -> Handler:
    """Run a web search and return the result with its sources."""

    def handle(session: LinkSession, request: Request) -> dict[str, Any]:
        if search is None:
            raise LinkOperationError(
                "not_configured", "Web search is not available on this build."
            )

        query = _require_text(request, "query")

        def emit(**data: Any) -> None:
            session.emit("research", **data)

        return search(query, emit)

    return handle


@dataclass(frozen=True)
class LinkOrigin:
    """
    Who is asking, over which path, under which ceiling.

    Built from the authenticated session, never from anything the phone says,
    and handed to whatever answers a request so the answer is produced under
    the same limits the link enforced.
    """

    device_id: str
    scope: LinkScope
    capabilities: frozenset[Capability]

    @property
    def remote(self) -> bool:
        return self.scope is not LinkScope.LOCAL_NETWORK

    @classmethod
    def of(cls, session: LinkSession) -> LinkOrigin:
        return cls(
            device_id=session.device.device_id,
            scope=session.scope,
            capabilities=resolve_capabilities(session.scope, session.grants),
        )


# What a phone outside the house is told when it asks Qronos to do something
# rather than to answer something. Persian, because it is shown to the user.
REMOTE_ACTION_REFUSAL = (
    "از بیرون از خانه فقط می‌توانم به پرسش‌ها پاسخ بدهم. باز کردن برنامه، "
    "کار با فایل‌ها، چاپ، جست‌وجو در مرورگر یا کنترل رایانه فقط از شبکهٔ "
    "خانه و با تأیید روی خود رایانه ممکن است."
)

# The words the remote answer path is told to live by. It cannot act in any
# case — nothing it calls can — but a model that is not told so may still
# write "done" in reply to "turn the computer off", which would be a lie the
# user acts on.
REMOTE_SYSTEM_PROMPT = (
    "You are Qronos, answering a question sent from the user's paired phone "
    "over the internet. In this conversation you cannot perform any action on "
    "the computer: you cannot open or control applications, read, write, "
    "delete or print files, browse the web, or change settings. Answer in "
    "words only, in the language of the question. If you are asked to do "
    "something on the computer, say plainly that it can only be done on the "
    "home network with confirmation at the computer, and never say or imply "
    "that you have done it."
)

# Printing is not a routing category of its own, so the router's markers do
# not catch it ("print the document" routes as an ordinary question). The
# remote path refuses it explicitly rather than inheriting that gap.
_EXTRA_ACTION_MARKERS = ("print", "printer", "چاپ", "پرینت", "پرینتر")

MAX_REMOTE_ANSWER_CHARS = 4_000

ChatFunction = Callable[[list[dict[str, str]]], str]


class ToolFreeAsk:
    """
    Answers in words, and can do nothing else.

    This is the only kind of ``ask`` a remote session can reach. It holds one
    callable — ``chat(messages) -> text`` — and nothing that could open an
    application, touch a file, print, browse or queue work for later. So a
    question from a phone outside the house cannot act on the PC however it is
    phrased: the capability ceiling is a property of what this object can
    reach, not of how well a model follows instructions.

    Two smaller guards sit on top, for honesty rather than safety. A request
    that reads as an action is answered with a fixed refusal and never reaches
    the model, so the model cannot claim to have done it. And the result always
    says ``performed_actions: []``, which the phone shows, so even a model that
    ignores its prompt is contradicted on screen by a fact.

    The runtime supplies ``chat``, built on the brain's plain chat call with
    the existing Fast model and settings. It must not route through the task
    router, the orchestrator or the queue.
    """

    def __init__(
        self,
        chat: ChatFunction,
        max_answer_chars: int = MAX_REMOTE_ANSWER_CHARS,
    ) -> None:
        if not callable(chat):
            raise TypeError("ToolFreeAsk needs a chat function")

        from core.task_router import TaskRouter

        self._chat = chat
        self._router = TaskRouter()
        self.max_answer_chars = max_answer_chars

    def looks_like_action(self, question: str) -> bool:
        from core.persian_text import contains_marker, normalise
        from core.task_router import TaskType

        decision = self._router.route(question)

        if decision.task_type not in {TaskType.FAST, TaskType.HEAVY}:
            return True

        return contains_marker(normalise(question).lower(), _EXTRA_ACTION_MARKERS)

    def __call__(
        self,
        question: str,
        emit: Callable[..., None],
        origin: LinkOrigin,
    ) -> dict[str, Any]:
        if self.looks_like_action(question):
            return {
                "answer": REMOTE_ACTION_REFUSAL,
                "refused_action": True,
                "performed_actions": [],
            }

        emit(phase="answering", progress=None)

        text = self._chat(
            [
                {"role": "system", "content": REMOTE_SYSTEM_PROMPT},
                {"role": "user", "content": question},
            ]
        )

        return {
            "answer": str(text).strip()[: self.max_answer_chars],
            "refused_action": False,
            "performed_actions": [],
        }


def ask_handler(
    ask: SearchFunction | None,
    remote_ask: ToolFreeAsk | None = None,
) -> Handler:
    """
    Put a question to the assistant.

    Two paths, chosen by the authenticated scope and nothing else. On the
    local network ``ask`` runs, as before; the permission engine still gates
    whatever it leads to. Over the relay only ``remote_ask`` may run, and it
    must be a ``ToolFreeAsk``: a plain function is refused when the handler is
    built, so a wiring mistake cannot hand the full assistant to a phone
    outside the house. Without one, remote questions are refused.
    """

    answer = question_answerer(ask, remote_ask)

    def handle(session: LinkSession, request: Request) -> dict[str, Any]:
        return answer(session, _require_text(request, "question"))

    return handle


def question_answerer(
    ask: SearchFunction | None,
    remote_ask: ToolFreeAsk | None = None,
) -> Callable[[LinkSession, str], dict[str, Any]]:
    """
    How a question becomes an answer for one session, typed or spoken.

    The scope decides the path and nothing else does: local sessions reach
    ``ask``; remote sessions reach only ``remote_ask``, which must be a
    ``ToolFreeAsk`` — checked here, when the handler is built, so a wiring
    mistake fails at startup rather than when a phone outside the house asks
    Qronos to delete something.
    """

    if remote_ask is not None and not isinstance(remote_ask, ToolFreeAsk):
        raise TypeError(
            "remote_ask must be a ToolFreeAsk; the remote path may not reach "
            "anything that can act on the computer"
        )

    def answer(session: LinkSession, question: str) -> dict[str, Any]:
        origin = LinkOrigin.of(session)

        def emit(**data: Any) -> None:
            session.emit("thinking", **data)

        if origin.remote:
            if remote_ask is None:
                raise LinkOperationError(
                    "not_configured",
                    "Asking from outside the house is not set up on this computer.",
                )

            return remote_ask(question, emit, origin)

        if ask is None:
            raise LinkOperationError(
                "not_configured",
                "Qronos cannot answer questions from the phone yet.",
            )

        if isinstance(ask, ToolFreeAsk):
            # Answer-only on the local network too, until the assistant's own
            # path with on-screen approval is wired to the link.
            return ask(question, emit, origin)

        return ask(question, emit)

    return answer


def default_handlers(
    status: StatusProvider | None = None,
    search: SearchFunction | None = None,
    ask: SearchFunction | None = None,
    relay: Any = None,
    remote_ask: ToolFreeAsk | None = None,
    transcribe: Any = None,
    speak: Any = None,
) -> dict[LinkOp, Handler]:
    """
    The operations the link serves.

    File, application and system operations are absent on purpose. They belong
    to the assistant's own action layer, and the link would be the wrong place
    to grow a second implementation of them. Until that layer is wired in, the
    server answers ``not_implemented``, which is accurate.

    ``relay`` is the configured ``RelayEndpoint`` when remote access is on, so
    ``ping`` can hand it to opted-in phones on the local network.
    ``remote_ask`` answers questions over the relay and must be a
    ``ToolFreeAsk``. ``transcribe`` and ``speak`` are the PC's own
    speech-to-text and text-to-speech, for push-to-talk
    (``core/link_voice.py``).
    """

    from core.link_voice import voice_handlers

    answer = question_answerer(ask, remote_ask)

    handlers: dict[LinkOp, Handler] = {
        LinkOp.PING: ping_handler if relay is None else remote_ping_handler(relay),
        LinkOp.STATUS: status_handler(status),
        LinkOp.SEARCH: search_handler(search),
        LinkOp.ASK: ask_handler(ask, remote_ask),
    }
    handlers.update(voice_handlers(transcribe, answer, speak))

    return handlers


def _sources_for(result: Any) -> list[str]:
    """
    The URLs behind an answer.

    Cited sources when the answer cited any, and everything read otherwise, so
    a refusal still shows the phone where Qronos looked. This mirrors what
    ``ResearchResult.provenance_text`` does for the rendered strip; listing
    only cited sources when there was no answer would show nothing at all.

    ``cited_urls`` belongs to the answer, not to the result. Reading it off the
    result returns an empty list and the phone silently loses its sources.
    """

    answer = getattr(result, "answer", None)
    cited = tuple(getattr(answer, "cited_urls", ()) or ())

    if cited:
        return list(cited)

    package = getattr(result, "package", None)
    items = getattr(package, "items", ()) or ()

    seen: list[str] = []

    for item in items:
        url = getattr(item, "url", "")

        if url and url not in seen:
            seen.append(url)

    return seen


def research_search_function(research: Any) -> SearchFunction:
    """
    Adapt a ``WebResearch`` instance to the search handler.

    This is the real integration point with the web research pipeline. The
    phone sees the same phases the desktop does — searching, reading,
    answering — including the honest absence of a progress fraction while the
    model is generating.
    """

    from core.web_query import build_query

    def run(query: str, emit: Callable[..., None]) -> dict[str, Any]:
        def forward(event: Any) -> None:
            emit(
                phase=getattr(event.phase, "value", str(event.phase)),
                done=getattr(event, "done", None),
                total=getattr(event, "total", None),
                progress=getattr(event, "progress", None),
            )

        previous = getattr(research, "on_event", None)
        research.on_event = forward

        try:
            result = research.research(build_query(query))
        finally:
            research.on_event = previous

        return {
            "ok": bool(result.ok),
            "text": result.render(),
            "sources": _sources_for(result),
            "used_pages": bool(result.used_pages),
        }

    return run


def main() -> None:
    """Show what a phone is told it may do, in each scope."""

    from core.link_capability import LinkScope

    print("operations served:",
          ", ".join(sorted(op.value for op in default_handlers())))
    print()

    for scope in LinkScope:
        capabilities = resolve_capabilities(scope)
        print(f"{scope.value:16}",
              ", ".join(sorted(item.value for item in capabilities)))


if __name__ == "__main__":
    main()
