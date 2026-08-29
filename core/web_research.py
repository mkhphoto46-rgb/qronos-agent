from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Callable

from core.web_answer import WebAnswer, build_prompt, validate_response
from core.web_evidence import EvidencePackage, build_package
from core.web_fetch import FetchReport, PageFetcher
from core.web_query import SearchQuery
from core.web_search import SearchReport, WebSearch


class ResearchPhase(Enum):
    """
    Phases of one research task, for the task-event stream.

    Named so the UI can show what is happening without inventing a percentage.
    Progress is only ever reported where a real count exists — pages read out
    of pages planned — and is left unset otherwise. A fabricated 0-to-100 timer
    is forbidden.
    """

    SEARCHING = "searching"
    READING = "reading"
    ANSWERING = "answering"
    COMPLETED = "completed"
    FAILED = "failed"


@dataclass(frozen=True)
class ResearchEvent:
    """
    One progress event.

    ``progress`` is ``None`` whenever there is nothing truthful to report, and
    a consumer must render that as an indeterminate state rather than guessing
    a number.
    """

    phase: ResearchPhase
    message: str = ""
    progress: float | None = None
    done: int = 0
    total: int = 0

    @property
    def is_measurable(self) -> bool:
        return self.progress is not None


EventSink = Callable[[ResearchEvent], None]


@dataclass(frozen=True)
class ResearchResult:
    """Everything one research task produced."""

    query: SearchQuery
    search: SearchReport
    fetch: FetchReport | None
    package: EvidencePackage
    answer: WebAnswer | None
    prompt: str = ""
    events: tuple[ResearchEvent, ...] = field(default_factory=tuple)

    @property
    def ok(self) -> bool:
        return self.answer is not None and self.answer.ok

    @property
    def used_pages(self) -> bool:
        return self.package.page_count > 0

    @property
    def provenance_text(self) -> str:
        """
        The source strip, built from what was actually cited.

        Falls back to every source read when nothing was cited, so a refusal
        still shows where Qronos looked.
        """
        from core.web_provenance import build_strip

        if self.answer is not None and self.answer.cited_urls:
            return build_strip(self.answer.cited_urls).render_persian()

        return self.package.provenance.render_persian()

    def render(self) -> str:
        if self.answer is None:
            return self.search.detail or "No answer was produced."

        return self.answer.render(provenance=self.provenance_text)

    def describe(self) -> str:
        return (
            f"{self.search.outcome.value} -> "
            f"{self.package.describe()} -> "
            f"{'answered' if self.ok else 'no answer'}"
        )


class WebResearch:
    """
    One question in, one answer out.

    The whole pipeline, and every decision in it is made by code rather than a
    model: whether to search, where to search, whether the snippets are enough,
    which pages to open, what the model may read, and whether its answer may be
    used.

    The model does exactly one thing — read the evidence and write an answer —
    and even that is checked afterwards.

    Two shortcuts matter for speed. A cached answer costs nothing. And when the
    search snippets are substantial enough, no page is opened at all: a search
    response carries a description per result, and those descriptions often
    contain the answer outright.

    ``answer_fn`` is the call into a brain. It is injected and optional: without
    it, this class runs the whole pipeline and stops at the prompt, which is how
    the search and evidence layers are exercised with no model present.
    """

    def __init__(
        self,
        search: WebSearch | None = None,
        fetcher: PageFetcher | None = None,
        answer_fn: Callable[[str], str] | None = None,
        on_event: EventSink | None = None,
        page_budget: int = 3,
    ) -> None:
        self.search = search if search is not None else WebSearch()
        self.fetcher = fetcher if fetcher is not None else PageFetcher()
        self.answer_fn = answer_fn
        self.on_event = on_event
        self.page_budget = page_budget

    # ------------------------------------------------------------------- run

    def research(
        self,
        query: SearchQuery,
        force_fetch: bool = False,
    ) -> ResearchResult:
        """
        Answer one question from the web.

        ``force_fetch`` opens pages even when the snippets look sufficient, for
        a caller that wants depth over speed.
        """
        events: list[ResearchEvent] = []

        def emit(event: ResearchEvent) -> None:
            events.append(event)

            if self.on_event is not None:
                self.on_event(event)

        emit(
            ResearchEvent(
                phase=ResearchPhase.SEARCHING,
                message="دارم می‌گردم",
            )
        )

        search_report = self.search.search(query)

        if not search_report.ok:
            emit(
                ResearchEvent(
                    phase=ResearchPhase.FAILED,
                    message=search_report.detail or "جواب پیدا نکردم",
                )
            )

            return ResearchResult(
                query=query,
                search=search_report,
                fetch=None,
                package=build_package(search_report),
                answer=None,
                events=tuple(events),
            )

        fetch_report: FetchReport | None = None
        needs_pages = force_fetch or not search_report.snippets_sufficient

        if needs_pages:
            candidates = tuple(
                result.url for result in search_report.results
            )

            emit(
                ResearchEvent(
                    phase=ResearchPhase.READING,
                    message="دارم می‌خونم",
                    progress=0.0,
                    done=0,
                    total=min(self.page_budget, len(candidates)),
                )
            )

            fetch_report = self.fetcher.fetch(
                candidates,
                budget=self.page_budget,
            )

            total = max(1, min(self.page_budget, len(candidates)))

            emit(
                ResearchEvent(
                    phase=ResearchPhase.READING,
                    message=fetch_report.describe(),
                    # A real count: pages read out of pages planned.
                    progress=min(1.0, fetch_report.count / total),
                    done=fetch_report.count,
                    total=total,
                )
            )

        package = build_package(search_report, fetch_report)

        if package.is_empty:
            emit(
                ResearchEvent(
                    phase=ResearchPhase.FAILED,
                    message="چیزی برای خوندن پیدا نشد",
                )
            )

            return ResearchResult(
                query=query,
                search=search_report,
                fetch=fetch_report,
                package=package,
                answer=None,
                events=tuple(events),
            )

        prompt = build_prompt(
            package,
            query.text,
            persian=query.is_persian,
        )

        emit(
            ResearchEvent(
                phase=ResearchPhase.ANSWERING,
                # No progress value: token generation has no honest fraction
                # to report, so the UI must show an indeterminate state.
                message="دارم جواب می‌دم",
            )
        )

        if self.answer_fn is None:
            # No brain wired up. The pipeline still produced everything a brain
            # would need, which is what makes this layer testable on its own.
            emit(
                ResearchEvent(
                    phase=ResearchPhase.COMPLETED,
                    message="prompt ready; no brain configured",
                )
            )

            return ResearchResult(
                query=query,
                search=search_report,
                fetch=fetch_report,
                package=package,
                answer=None,
                prompt=prompt,
                events=tuple(events),
            )

        try:
            raw = self.answer_fn(prompt)
        except Exception as exc:
            emit(
                ResearchEvent(
                    phase=ResearchPhase.FAILED,
                    message=f"brain failed: {exc}",
                )
            )

            return ResearchResult(
                query=query,
                search=search_report,
                fetch=fetch_report,
                package=package,
                answer=None,
                prompt=prompt,
                events=tuple(events),
            )

        answer = validate_response(
            raw,
            package,
            persian=query.is_persian,
        )

        emit(
            ResearchEvent(
                phase=(
                    ResearchPhase.COMPLETED
                    if answer.ok
                    else ResearchPhase.FAILED
                ),
                message=(
                    "done"
                    if answer.ok
                    else (answer.detail or "no answer")
                ),
            )
        )

        return ResearchResult(
            query=query,
            search=search_report,
            fetch=fetch_report,
            package=package,
            answer=answer,
            prompt=prompt,
            events=tuple(events),
        )

    # -------------------------------------------------------------- reporting

    def budget_status(self) -> str:
        return self.search.budget_status()

    def close(self) -> None:
        """Release the persistent cache and friction-memory connections."""
        self.search.cache.close()
        self.fetcher.friction.close()


def main() -> None:
    """Run the full pipeline live, stopping at the prompt. Uses real budget."""
    import sys

    from core.web_cache import WebCache
    from core.web_friction import FrictionMemory
    from core.web_query import build_query

    utterance = " ".join(sys.argv[1:]) or "برام قیمت دلار امروز رو سرچ کن"

    def show(event: ResearchEvent) -> None:
        progress = (
            f" {event.done}/{event.total}"
            if event.is_measurable
            else " (indeterminate)"
        )
        print(f"  [{event.phase.value}]{progress} {event.message}")

    research = WebResearch(
        search=WebSearch(cache=WebCache(path=":memory:")),
        fetcher=PageFetcher(friction=FrictionMemory(path=":memory:")),
        on_event=show,
    )

    query = build_query(utterance)

    print(f"utterance : {utterance}")
    print(f"query     : {query.text}")
    print(f"budget    : {research.budget_status()}")
    print()

    result = research.research(query)

    print()
    print(result.describe())
    print(f"pages used: {result.used_pages}")
    print()
    print(result.provenance_text)
    print()
    print(f"prompt length: {len(result.prompt)} chars")


if __name__ == "__main__":
    main()
