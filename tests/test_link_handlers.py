from __future__ import annotations

import unittest
from dataclasses import dataclass, field
from typing import Any

from core.link_capability import Capability, LinkOp, LinkScope
from core.link_devices import DeviceRegistry
from core.link_handlers import (
    _require_text,
    ask_handler,
    default_handlers,
    ping_handler,
    research_search_function,
    search_handler,
    status_handler,
)
from core.link_protocol import Request
from core.link_server import LinkOperationError


@dataclass
class StubSession:
    """
    Only what a handler actually touches.

    A real ``LinkSession`` needs a TLS socket, and none of these tests need
    one — the handlers are where the link stops and the application begins.
    """

    scope: LinkScope = LinkScope.LOCAL_NETWORK
    grants: frozenset[Capability] | None = None
    emitted: list[dict[str, Any]] = field(default_factory=list)
    device: Any = None

    def __post_init__(self) -> None:
        if self.device is None:
            registry = DeviceRegistry(path=None)
            self.device = registry.create("stub phone")

    def emit(self, topic: str, **data: Any) -> None:
        self.emitted.append({"topic": topic, **data})


def request(op: str = "search", **params: Any) -> Request:
    return Request(id=1, op=op, params=params)


class TestDefaults(unittest.TestCase):
    def test_the_served_operations_are_the_documented_ones(self) -> None:
        self.assertEqual(
            set(default_handlers()),
            {LinkOp.PING, LinkOp.STATUS, LinkOp.SEARCH, LinkOp.ASK},
        )

    def test_file_and_system_operations_are_absent(self) -> None:
        # They belong to the assistant's own action layer. The link is the
        # wrong place to grow a second implementation of them, so the server
        # answers not_implemented, which is accurate.
        served = set(default_handlers())

        for op in (
            LinkOp.READ_FILE,
            LinkOp.WRITE_FILE,
            LinkOp.DELETE_FILE,
            LinkOp.RUN_APP,
            LinkOp.SYSTEM_CONTROL,
        ):
            with self.subTest(op=op):
                self.assertNotIn(op, served)

    def test_device_management_is_never_served(self) -> None:
        served = set(default_handlers())

        self.assertNotIn(LinkOp.LIST_DEVICES, served)
        self.assertNotIn(LinkOp.REVOKE_DEVICE, served)

    def test_every_served_operation_is_a_real_operation(self) -> None:
        for op in default_handlers():
            with self.subTest(op=op):
                self.assertIsInstance(op, LinkOp)


class TestPing(unittest.TestCase):
    def test_it_reports_the_local_capabilities(self) -> None:
        result = ping_handler(StubSession(), request("ping"))

        self.assertIn("search_web", result["capabilities"])
        self.assertIn("run_application", result["capabilities"])
        self.assertEqual(result["scope"], "local_network")

    def test_it_reports_the_narrower_remote_capabilities(self) -> None:
        # So the phone can grey out what it cannot ask for rather than
        # discovering it by being refused.
        result = ping_handler(
            StubSession(scope=LinkScope.REMOTE_TUNNEL), request("ping")
        )

        self.assertIn("search_web", result["capabilities"])
        self.assertNotIn("run_application", result["capabilities"])

    def test_it_reflects_a_narrowed_device(self) -> None:
        result = ping_handler(
            StubSession(grants=frozenset({Capability.ASK})), request("ping")
        )

        self.assertEqual(result["capabilities"], ["ask"])

    def test_it_names_the_device_and_the_assistant(self) -> None:
        session = StubSession()

        result = ping_handler(session, request("ping"))

        self.assertEqual(result["device"], session.device.device_id)
        self.assertEqual(result["name"], "Qronos")


class TestStatus(unittest.TestCase):
    def test_without_a_provider_it_says_so_rather_than_inventing(self) -> None:
        result = status_handler(None)(StubSession(), request("status"))

        self.assertEqual(result["link"], "ok")
        self.assertIn("no status provider", result["detail"])

    def test_a_provider_supplies_the_answer(self) -> None:
        handler = status_handler(lambda: {"free_gb": 42})

        result = handler(StubSession(), request("status"))

        self.assertEqual(result, {"free_gb": 42})

    def test_a_failing_provider_becomes_a_refusal_not_a_crash(self) -> None:
        def explode() -> dict[str, Any]:
            raise RuntimeError("psutil exploded")

        handler = status_handler(explode)

        with self.assertRaises(LinkOperationError) as caught:
            handler(StubSession(), request("status"))

        self.assertEqual(caught.exception.code, "status_unavailable")
        self.assertNotIn("psutil", caught.exception.message)


class TestSearch(unittest.TestCase):
    def test_without_a_search_function_it_refuses(self) -> None:
        # Better than telling the phone "searching" when nothing can search.
        with self.assertRaises(LinkOperationError) as caught:
            search_handler(None)(StubSession(), request(query="x"))

        self.assertEqual(caught.exception.code, "not_configured")

    def test_it_passes_the_query_through(self) -> None:
        seen: dict[str, str] = {}

        def search(query: str, emit) -> dict[str, Any]:
            seen["query"] = query

            return {"ok": True}

        search_handler(search)(StubSession(), request(query="  قیمت دلار  "))

        self.assertEqual(seen["query"], "قیمت دلار")

    def test_progress_reaches_the_session(self) -> None:
        def search(query: str, emit) -> dict[str, Any]:
            emit(phase="searching")
            emit(phase="reading", done=1, total=3)

            return {"ok": True}

        session = StubSession()
        search_handler(search)(session, request(query="x"))

        self.assertEqual(
            [item["phase"] for item in session.emitted],
            ["searching", "reading"],
        )
        self.assertEqual(session.emitted[0]["topic"], "research")

    def test_a_missing_query_is_a_bad_request(self) -> None:
        with self.assertRaises(LinkOperationError) as caught:
            search_handler(lambda q, e: {})(StubSession(), request())

        self.assertEqual(caught.exception.code, "bad_request")

    def test_a_blank_query_is_a_bad_request(self) -> None:
        for value in ("", "   ", "\n"):
            with self.subTest(value=value):
                with self.assertRaises(LinkOperationError):
                    search_handler(lambda q, e: {})(
                        StubSession(), request(query=value)
                    )

    def test_a_non_string_query_is_a_bad_request(self) -> None:
        for value in (42, None, ["a"], {"a": 1}, True):
            with self.subTest(value=value):
                with self.assertRaises(LinkOperationError):
                    search_handler(lambda q, e: {})(
                        StubSession(), request(query=value)
                    )

    def test_an_absurdly_long_query_is_refused(self) -> None:
        with self.assertRaises(LinkOperationError) as caught:
            search_handler(lambda q, e: {})(
                StubSession(), request(query="x" * 5_000)
            )

        self.assertEqual(caught.exception.code, "bad_request")


class TestAsk(unittest.TestCase):
    def test_it_refuses_by_default(self) -> None:
        # Qronos has no wired-up model and no Persian speech, so there is
        # nothing honest for this to return yet.
        with self.assertRaises(LinkOperationError) as caught:
            ask_handler(None)(StubSession(), request("ask", question="x"))

        self.assertEqual(caught.exception.code, "not_configured")

    def test_a_wired_up_function_is_called(self) -> None:
        handler = ask_handler(lambda question, emit: {"answer": question})

        result = handler(StubSession(), request("ask", question="چطوری؟"))

        self.assertEqual(result["answer"], "چطوری؟")

    def test_progress_uses_the_thinking_topic(self) -> None:
        def ask(question: str, emit) -> dict[str, Any]:
            emit(phase="answering")

            return {}

        session = StubSession()
        ask_handler(ask)(session, request("ask", question="x"))

        self.assertEqual(session.emitted[0]["topic"], "thinking")

    def test_a_missing_question_is_a_bad_request(self) -> None:
        with self.assertRaises(LinkOperationError):
            ask_handler(lambda q, e: {})(StubSession(), request("ask"))


class TestRequireText(unittest.TestCase):
    def test_it_trims(self) -> None:
        self.assertEqual(_require_text(request(q="  x  "), "q"), "x")

    def test_the_limit_is_per_field(self) -> None:
        with self.assertRaises(LinkOperationError):
            _require_text(request(q="xx"), "q", limit=1)

        self.assertEqual(_require_text(request(q="x"), "q", limit=1), "x")

    def test_the_message_names_the_field(self) -> None:
        with self.assertRaises(LinkOperationError) as caught:
            _require_text(request(), "query")

        self.assertIn("query", caught.exception.message)


@dataclass
class StubItem:
    url: str


@dataclass
class StubPackage:
    items: tuple[StubItem, ...]


@dataclass
class StubAnswer:
    cited_urls: tuple[str, ...]


class StubResearchResult:
    """
    Mirrors the real ``ResearchResult``, including that ``cited_urls`` lives on
    the *answer* rather than on the result.

    An earlier version of this stub exposed ``cited_urls`` directly, which is
    what the adapter wrongly read. The test passed and the phone silently
    received no sources at all. A stub that encodes the assumption under test
    proves nothing, so this one follows the real shape.
    """

    def __init__(
        self,
        ok: bool = True,
        cited: tuple[str, ...] = ("https://tgju.org/",),
        read: tuple[str, ...] = (
            "https://tgju.org/",
            "https://bon-bast.com/",
        ),
    ) -> None:
        self.ok = ok
        self.used_pages = True
        self.answer = StubAnswer(cited_urls=cited) if ok else None
        self.package = StubPackage(items=tuple(StubItem(url) for url in read))

    def render(self) -> str:
        return "قیمت دلار امروز ... [1]"


class StubEvent:
    def __init__(self, phase: str, done: int | None, total: int | None,
                 progress: float | None) -> None:
        class Phase:
            value = phase

        self.phase = Phase()
        self.done = done
        self.total = total
        self.progress = progress


class StubResearch:
    """Stands in for ``WebResearch`` without a network or a model."""

    def __init__(self, result: StubResearchResult | None = None) -> None:
        self.on_event = None
        self.queries: list[Any] = []
        self.result = result if result is not None else StubResearchResult()

    def research(self, query: Any) -> StubResearchResult:
        self.queries.append(query)

        if self.on_event is not None:
            self.on_event(StubEvent("searching", None, None, None))
            self.on_event(StubEvent("reading", 2, 3, 2 / 3))
            self.on_event(StubEvent("answering", None, None, None))

        return self.result


class TestResearchAdapter(unittest.TestCase):
    def test_it_returns_the_rendered_answer_and_its_sources(self) -> None:
        session = StubSession()
        search = research_search_function(StubResearch())

        result = search("قیمت دلار", lambda **data: session.emit("research", **data))

        self.assertTrue(result["ok"])
        self.assertIn("قیمت دلار", result["text"])
        self.assertEqual(result["sources"], ["https://tgju.org/"])
        self.assertTrue(result["used_pages"])

    def test_the_sources_come_from_the_answer_not_the_result(self) -> None:
        # cited_urls belongs to the answer. Reading it off the result yields an
        # empty list and the phone loses its sources without any error.
        search = research_search_function(StubResearch())

        result = search("x", lambda **data: None)

        self.assertEqual(result["sources"], ["https://tgju.org/"])

    def test_without_an_answer_every_source_read_is_listed(self) -> None:
        # A refusal should still show the phone where Qronos looked, which is
        # what the rendered provenance strip does.
        search = research_search_function(
            StubResearch(StubResearchResult(ok=False))
        )

        result = search("x", lambda **data: None)

        self.assertFalse(result["ok"])
        self.assertEqual(
            result["sources"],
            ["https://tgju.org/", "https://bon-bast.com/"],
        )

    def test_a_repeated_url_is_listed_once(self) -> None:
        search = research_search_function(
            StubResearch(
                StubResearchResult(
                    ok=False,
                    read=("https://a.example/", "https://a.example/"),
                )
            )
        )

        result = search("x", lambda **data: None)

        self.assertEqual(result["sources"], ["https://a.example/"])

    def test_research_phases_are_forwarded(self) -> None:
        session = StubSession()
        search = research_search_function(StubResearch())

        search("x", lambda **data: session.emit("research", **data))

        self.assertEqual(
            [item["phase"] for item in session.emitted],
            ["searching", "reading", "answering"],
        )

    def test_a_real_count_survives_the_trip(self) -> None:
        session = StubSession()
        search = research_search_function(StubResearch())

        search("x", lambda **data: session.emit("research", **data))

        reading = session.emitted[1]

        self.assertEqual(reading["done"], 2)
        self.assertEqual(reading["total"], 3)

    def test_an_indeterminate_phase_forwards_no_fraction(self) -> None:
        # Token generation has no honest progress fraction, and the phone must
        # be able to show an indeterminate state rather than guess.
        session = StubSession()
        search = research_search_function(StubResearch())

        search("x", lambda **data: session.emit("research", **data))

        self.assertIsNone(session.emitted[2]["progress"])

    def test_the_previous_event_handler_is_restored(self) -> None:
        # The desktop UI may already be listening; the phone borrowing the
        # stream must not permanently steal it.
        research = StubResearch()
        sentinel = object()
        research.on_event = sentinel  # type: ignore[assignment]

        research_search_function(research)("x", lambda **data: None)

        self.assertIs(research.on_event, sentinel)

    def test_the_utterance_is_turned_into_a_query_object(self) -> None:
        research = StubResearch()

        research_search_function(research)("قیمت دلار", lambda **data: None)

        self.assertEqual(len(research.queries), 1)
        self.assertNotIsInstance(research.queries[0], str)


if __name__ == "__main__":
    unittest.main()
