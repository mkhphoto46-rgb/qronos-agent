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
from typing import Any, Callable, Protocol

from core.config import CONFIG
from core.link_capability import LinkOp, resolve_capabilities
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


def ask_handler(ask: SearchFunction | None) -> Handler:
    """Put a question to the assistant."""

    def handle(session: LinkSession, request: Request) -> dict[str, Any]:
        if ask is None:
            raise LinkOperationError(
                "not_configured",
                "Qronos cannot answer questions from the phone yet.",
            )

        question = _require_text(request, "question")

        def emit(**data: Any) -> None:
            session.emit("thinking", **data)

        return ask(question, emit)

    return handle


def default_handlers(
    status: StatusProvider | None = None,
    search: SearchFunction | None = None,
    ask: SearchFunction | None = None,
) -> dict[LinkOp, Handler]:
    """
    The operations Layer 1 serves.

    File, application and system operations are absent on purpose. They belong
    to the assistant's own action layer, and the link would be the wrong place
    to grow a second implementation of them. Until that layer is wired in, the
    server answers ``not_implemented``, which is accurate.
    """

    return {
        LinkOp.PING: ping_handler,
        LinkOp.STATUS: status_handler(status),
        LinkOp.SEARCH: search_handler(search),
        LinkOp.ASK: ask_handler(ask),
    }


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
