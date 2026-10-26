"""Trace context: the Voyage span is current while the SDK sends HTTP, and nests."""

from __future__ import annotations

import asyncio

import pytest
from opentelemetry import trace as trace_api

pytest.importorskip("voyageai", reason="voyageai must be installed to test its instrumentor")

from voyageai.api_resources import api_requestor  # noqa: E402

from _support import (  # noqa: E402
    DOCUMENTS,
    EMBED_MODEL,
    QUERY,
    RERANK_MODEL,
    TEXTS,
    FakeVoyage,
    async_client,
    client,
    instrumented,
)


@pytest.fixture()
def fake():
    with FakeVoyage() as server:
        yield server


def _current_span_id() -> int:
    return trace_api.get_current_span().get_span_context().span_id


@pytest.fixture()
def seen_by_http(monkeypatch):
    """Record the current span id where voyageai sends HTTP.

    ``APIRequestor.request_raw`` (requests) and ``arequest_raw`` (aiohttp) are
    where an HTTP client instrumentation would start its span, so the id seen
    there is the parent an HTTP child span would get.
    """
    seen = []
    request_raw = api_requestor.APIRequestor.request_raw
    arequest_raw = api_requestor.APIRequestor.arequest_raw

    def sync_raw(self, *args, **kwargs):
        seen.append(_current_span_id())
        return request_raw(self, *args, **kwargs)

    async def async_raw(self, *args, **kwargs):
        seen.append(_current_span_id())
        return await arequest_raw(self, *args, **kwargs)

    monkeypatch.setattr(api_requestor.APIRequestor, "request_raw", sync_raw)
    monkeypatch.setattr(api_requestor.APIRequestor, "arequest_raw", async_raw)
    return seen


def test_sync_vendor_http_runs_inside_the_voyage_span(fake, seen_by_http):
    with instrumented() as traced:
        voyage = client(fake)
        voyage.embed(TEXTS, model=EMBED_MODEL)
        voyage.rerank(QUERY, DOCUMENTS, model=RERANK_MODEL)

    span_ids = [span.context.span_id for span in traced.spans()]
    assert len(span_ids) == 2
    assert seen_by_http == span_ids
    # Nothing is left current once the calls return.
    assert _current_span_id() == 0


def test_async_vendor_http_runs_inside_the_voyage_span(fake, seen_by_http):
    async def calls() -> None:
        voyage = async_client(fake)
        await voyage.embed(TEXTS, model=EMBED_MODEL)
        await voyage.rerank(QUERY, DOCUMENTS, model=RERANK_MODEL)
        assert _current_span_id() == 0

    with instrumented() as traced:
        asyncio.run(calls())

    span_ids = [span.context.span_id for span in traced.spans()]
    assert len(span_ids) == 2
    assert seen_by_http == span_ids


def test_an_http_child_span_nests_under_the_voyage_span(fake, monkeypatch):
    """A span started at the HTTP layer (as an HTTP instrumentor would) is a child."""
    with instrumented() as traced:
        request_raw = api_requestor.APIRequestor.request_raw
        http_tracer = traced.provider.get_tracer("http")

        def traced_raw(self, *args, **kwargs):
            with http_tracer.start_as_current_span("POST"):
                return request_raw(self, *args, **kwargs)

        monkeypatch.setattr(api_requestor.APIRequestor, "request_raw", traced_raw)
        client(fake).embed(TEXTS, model=EMBED_MODEL)

    by_name = {span.name: span for span in traced.spans()}
    assert by_name["POST"].parent.span_id == by_name["voyage.embed"].context.span_id
    # Usage lives on the Voyage span only, once.
    assert "gen_ai.usage.total_tokens" not in (by_name["POST"].attributes or {})
    assert by_name["voyage.embed"].attributes["gen_ai.usage.total_tokens"] == 14


def test_voyage_spans_are_children_of_the_active_span(fake):
    async def call() -> None:
        await async_client(fake).rerank(QUERY, DOCUMENTS, model=RERANK_MODEL)

    with instrumented() as traced:
        tracer = traced.provider.get_tracer("agent")
        with tracer.start_as_current_span("agent.step") as parent:
            client(fake).embed(TEXTS, model=EMBED_MODEL)
            asyncio.run(call())

    by_name = {span.name: span for span in traced.spans()}
    agent = by_name["agent.step"]
    assert agent.context.span_id == parent.get_span_context().span_id
    for name in ("voyage.embed", "voyage.rerank"):
        assert by_name[name].parent.span_id == agent.context.span_id
        assert by_name[name].context.trace_id == agent.context.trace_id


def test_a_voyage_span_is_a_root_span_without_a_parent(fake):
    with instrumented() as traced:
        client(fake).embed(TEXTS, model=EMBED_MODEL)

    assert traced.one().parent is None


def test_session_context_attributes_are_attached(fake):
    from fi_instrumentation import using_session

    with instrumented() as traced:
        with using_session("session-123"):
            client(fake).embed(TEXTS, model=EMBED_MODEL)

    assert traced.one().attributes["session.id"] == "session-123"


def test_suppress_tracing_records_no_span(fake):
    from fi_instrumentation import suppress_tracing

    with instrumented() as traced:
        with suppress_tracing():
            result = client(fake).embed(TEXTS, model=EMBED_MODEL)

    assert len(result.embeddings) == 2
    assert traced.spans() == []
