"""Trace context: the Parallel span is current while the SDK sends HTTP, and nests."""

from __future__ import annotations

import asyncio

import pytest

pytest.importorskip("parallel", reason="parallel-web must be installed to test its instrumentor")

import httpx  # noqa: E402
from opentelemetry import trace as trace_api  # noqa: E402

from _parallel_support import (  # noqa: E402
    FakeParallel,
    async_client,
    instrumented,
    sync_client,
)


@pytest.fixture()
def fake():
    with FakeParallel() as server:
        yield server


def _current_span_id() -> int:
    return trace_api.get_current_span().get_span_context().span_id


def test_sync_http_request_runs_inside_the_parallel_span(fake):
    # An httpx request hook runs where an HTTP client instrumentor would start
    # its span, so the span current there is the parent an HTTP span gets.
    seen = []
    http_client = httpx.Client(event_hooks={"request": [lambda _: seen.append(_current_span_id())]})
    with instrumented() as traced:
        client = sync_client(fake, http_client=http_client)
        client.search(search_queries=["q"])
        client.extract(urls=["https://x.example/a"])

    span_ids = [span.context.span_id for span in traced.spans()]
    assert len(span_ids) == 2
    assert seen == span_ids
    # Nothing is left current once the calls return.
    assert _current_span_id() == 0


def test_async_http_request_runs_inside_the_parallel_span(fake):
    seen = []

    async def hook(_request) -> None:
        seen.append(_current_span_id())

    async def call() -> None:
        client = async_client(fake, http_client=httpx.AsyncClient(event_hooks={"request": [hook]}))
        try:
            await client.search(search_queries=["q"])
            await client.extract(urls=["https://x.example/a"])
        finally:
            await client.close()

    with instrumented() as traced:
        asyncio.run(call())

    span_ids = [span.context.span_id for span in traced.spans()]
    assert len(span_ids) == 2
    assert seen == span_ids


def test_parallel_spans_are_children_of_the_active_span(fake):
    async def call() -> None:
        client = async_client(fake)
        try:
            await client.extract(urls=["https://x.example/a"])
        finally:
            await client.close()

    with instrumented() as traced:
        tracer = traced.provider.get_tracer("agent")
        with tracer.start_as_current_span("agent.step") as parent:
            sync_client(fake).search(search_queries=["q"])
            asyncio.run(call())

    by_name = {span.name: span for span in traced.spans()}
    agent = by_name["agent.step"]
    assert agent.context.span_id == parent.get_span_context().span_id
    for name in ("parallel.search", "parallel.extract"):
        assert by_name[name].parent.span_id == agent.context.span_id
        assert by_name[name].context.trace_id == agent.context.trace_id


def test_parallel_span_is_a_root_span_without_a_parent(fake):
    with instrumented() as traced:
        sync_client(fake).search(search_queries=["q"])

    assert traced.one().parent is None


def test_one_span_per_call_with_sdk_retries(fake):
    # parallel-web retries 5xx inside one search() call; that is one span.
    with instrumented() as traced:
        with pytest.raises(Exception):
            sync_client(fake, max_retries=1).search(search_queries=["fail-500"])

    assert len(fake.calls) == 2
    assert [span.name for span in traced.spans()] == ["parallel.search"]
