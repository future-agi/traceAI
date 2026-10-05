"""Span contract for TavilyClient / AsyncTavilyClient search and extract.

The real tavily-python client makes real HTTP calls to the loopback fake
(``api_base_url=fake.origin``); spans go to an in-memory provider so the
wrapper's own status is visible (see _support).
"""

from __future__ import annotations

import asyncio
import inspect
from typing import Any, List

import pytest

pytest.importorskip("tavily", reason="tavily-python must be installed to test its instrumentor")

from opentelemetry import trace as trace_api  # noqa: E402
from opentelemetry.trace import StatusCode  # noqa: E402

from _support import attrs, instrumented  # noqa: E402
from _tavily_fake import CONTENT_MARKERS, TAVILY_KEY, FakeTavily  # noqa: E402

QUERY = "who maintains OpenTelemetry?"
URLS = ["https://example.com/a", "https://example.com/b", "https://example.com/fail-c"]


@pytest.fixture()
def fake():
    with FakeTavily() as server:
        yield server


def _sync_client(fake: FakeTavily) -> Any:
    from tavily import TavilyClient

    return TavilyClient(api_key=TAVILY_KEY, api_base_url=fake.origin)


def _async_client(fake: FakeTavily) -> Any:
    from tavily import AsyncTavilyClient

    return AsyncTavilyClient(api_key=TAVILY_KEY, api_base_url=fake.origin)


async def _async_call(fake: FakeTavily, method: str, *args: Any, **kwargs: Any) -> Any:
    client = _async_client(fake)
    try:
        return await getattr(client, method)(*args, **kwargs)
    finally:
        await client.close()


def _assert_no_content(traced: Any) -> None:
    wire = traced.wire()
    for marker in CONTENT_MARKERS:
        assert marker not in wire, marker
    assert TAVILY_KEY not in wire
    for url in URLS:
        assert url not in wire, url


# --- search -------------------------------------------------------------------


def test_sync_search_emits_one_tool_span(fake):
    with instrumented() as traced:
        result = _sync_client(fake).search(QUERY, max_results=3)

    assert len(result["results"]) == 3
    assert fake.paths() == ["/search"]
    span = traced.one()
    assert span.name == "tavily.search"
    assert attrs(span) == {
        "gen_ai.span.kind": "TOOL",
        "gen_ai.tool.name": "tavily.search",
        "input.value": QUERY,
        "tavily.result_count": 3,
    }
    assert span.status.status_code is StatusCode.OK
    assert span.parent is None
    _assert_no_content(traced)


def test_async_search_matches_sync(fake):
    with instrumented() as traced:
        result = asyncio.run(_async_call(fake, "search", query=QUERY, max_results=3))

    assert len(result["results"]) == 3
    span = traced.one()
    assert span.name == "tavily.search"
    assert attrs(span) == {
        "gen_ai.span.kind": "TOOL",
        "gen_ai.tool.name": "tavily.search",
        "input.value": QUERY,
        "tavily.result_count": 3,
    }
    assert span.status.status_code is StatusCode.OK
    _assert_no_content(traced)


# --- extract ------------------------------------------------------------------


def test_sync_extract_counts_urls_and_results_without_recording_urls(fake):
    with instrumented() as traced:
        result = _sync_client(fake).extract(URLS)

    assert len(result["results"]) == 2 and len(result["failed_results"]) == 1
    span = traced.one()
    assert span.name == "tavily.extract"
    # No query was given, so there is no input.value; URLs are never recorded.
    assert attrs(span) == {
        "gen_ai.span.kind": "TOOL",
        "gen_ai.tool.name": "tavily.extract",
        "tavily.url_count": 3,
        "tavily.result_count": 2,
        "tavily.failed_result_count": 1,
    }
    # Partial failures are part of a successful response.
    assert span.status.status_code is StatusCode.OK
    _assert_no_content(traced)


def test_async_extract_matches_sync(fake):
    with instrumented() as traced:
        asyncio.run(_async_call(fake, "extract", URLS))

    span = traced.one()
    assert span.name == "tavily.extract"
    assert attrs(span) == {
        "gen_ai.span.kind": "TOOL",
        "gen_ai.tool.name": "tavily.extract",
        "tavily.url_count": 3,
        "tavily.result_count": 2,
        "tavily.failed_result_count": 1,
    }
    assert span.status.status_code is StatusCode.OK
    _assert_no_content(traced)


@pytest.mark.parametrize("mode", ["sync", "async"])
def test_extract_records_its_rerank_query_as_input(fake, mode):
    with instrumented() as traced:
        if mode == "sync":
            _sync_client(fake).extract(URLS[0], query="pricing page")
        else:
            asyncio.run(_async_call(fake, "extract", URLS[0], query="pricing page"))

    values = attrs(traced.one())
    assert values["input.value"] == "pricing page"
    # A single URL string counts as one URL.
    assert values["tavily.url_count"] == 1


def test_extract_reads_a_positional_query(fake):
    with instrumented() as traced:
        # extract(urls, include_images, extract_depth, format, timeout,
        #         include_favicon, include_usage, query)
        _sync_client(fake).extract(URLS[:1], None, None, None, 30, None, None, "positional")

    assert attrs(traced.one())["input.value"] == "positional"


# --- context ------------------------------------------------------------------


@pytest.mark.parametrize("mode", ["sync", "async"])
def test_span_is_a_child_of_the_active_span(fake, mode):
    with instrumented() as traced:
        tracer = traced.provider.get_tracer("test")
        with tracer.start_as_current_span("agent-step") as parent:
            if mode == "sync":
                _sync_client(fake).search(QUERY)
            else:
                asyncio.run(_async_call(fake, "search", QUERY))

    by_name = {span.name: span for span in traced.spans()}
    child = by_name["tavily.search"]
    assert child.parent is not None
    assert child.parent.span_id == parent.get_span_context().span_id
    assert child.context.trace_id == parent.get_span_context().trace_id


def test_span_is_current_while_requests_sends(fake):
    """An HTTP-client span started during the call would nest under tavily.search."""
    import requests
    from tavily import TavilyClient

    seen: List[Any] = []

    def hook(response: Any, *args: Any, **kwargs: Any) -> Any:
        seen.append(trace_api.get_current_span().get_span_context())
        return response

    session = requests.Session()
    session.hooks["response"].append(hook)
    with instrumented() as traced:
        client = TavilyClient(api_key=TAVILY_KEY, api_base_url=fake.origin, session=session)
        client.search(QUERY)

    span = traced.one()
    assert [context.span_id for context in seen] == [span.context.span_id]


def test_span_is_current_while_httpx_sends(fake):
    import httpx
    from tavily import AsyncTavilyClient

    seen: List[Any] = []

    async def hook(request: Any) -> None:
        seen.append(trace_api.get_current_span().get_span_context())

    async def call() -> None:
        async with httpx.AsyncClient(
            base_url=fake.origin,
            headers={"Authorization": "Bearer " + TAVILY_KEY},
            event_hooks={"request": [hook]},
        ) as http:
            await AsyncTavilyClient(client=http).search(QUERY)

    with instrumented() as traced:
        asyncio.run(call())

    span = traced.one()
    assert [context.span_id for context in seen] == [span.context.span_id]
    # Outside the call the span is no longer current.
    assert trace_api.get_current_span().get_span_context().span_id != span.context.span_id


def test_async_methods_stay_coroutine_functions(fake):
    from tavily import AsyncTavilyClient

    with instrumented():
        assert inspect.iscoroutinefunction(AsyncTavilyClient.search)
        assert inspect.iscoroutinefunction(AsyncTavilyClient.extract)


def test_deprecated_client_subclass_is_traced_through_tavily_client(fake):
    """``tavily.Client`` inherits TavilyClient.search, so it is covered too."""
    import warnings

    from tavily import Client

    with instrumented() as traced:
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", DeprecationWarning)
            client = Client(TAVILY_KEY)
        # Client(kwargs) takes only the key; the URL is read on every call.
        client.base_url = fake.origin
        client.search(QUERY)

    assert fake.paths() == ["/search"]
    assert [span.name for span in traced.spans()] == ["tavily.search"]
