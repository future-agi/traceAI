"""Trace context: the Exa span is current during the vendor call and nests."""

from __future__ import annotations

import asyncio

import pytest
from opentelemetry import trace as trace_api

pytest.importorskip("exa_py", reason="exa-py must be installed to test its instrumentor")

from _support import EXA_KEY, FakeExa, instrumented  # noqa: E402
from exa_py import AsyncExa, Exa  # noqa: E402


@pytest.fixture()
def fake():
    with FakeExa() as server:
        yield server


def _current_span_id() -> int:
    return trace_api.get_current_span().get_span_context().span_id


@pytest.fixture()
def seen_by_http(monkeypatch):
    """Record the current span id where exa-py sends HTTP (Exa.request / async_request).

    An HTTP client instrumentor starts its span at that point, so the id seen
    there is the parent an HTTP child span would get.
    """
    seen = []
    sync_request = Exa.request
    async_request = AsyncExa.async_request

    def request(self, *args, **kwargs):
        seen.append(_current_span_id())
        return sync_request(self, *args, **kwargs)

    async def async_request_(self, *args, **kwargs):
        seen.append(_current_span_id())
        return await async_request(self, *args, **kwargs)

    monkeypatch.setattr(Exa, "request", request)
    monkeypatch.setattr(AsyncExa, "async_request", async_request_)
    return seen


def test_sync_vendor_http_runs_inside_the_exa_span(fake, seen_by_http):
    with instrumented() as traced:
        client = Exa(api_key=EXA_KEY, base_url=fake.origin)
        client.search("q")
        client.answer("q")
        client.get_contents("https://example.com/0")
        list(client.stream_search("q"))
        list(client.stream_answer("q"))

    span_ids = [span.context.span_id for span in traced.spans()]
    assert len(span_ids) == 5
    assert seen_by_http == span_ids
    # Nothing is left current once the calls return.
    assert _current_span_id() == 0


def test_async_vendor_http_runs_inside_the_exa_span(fake, seen_by_http):
    async def call() -> None:
        client = AsyncExa(api_key=EXA_KEY, api_base=fake.origin)
        try:
            await client.search("q")
            await client.get_contents(["https://example.com/0"])
            async for _ in await client.stream_answer("q"):
                pass
        finally:
            await client.client.aclose()

    with instrumented() as traced:
        asyncio.run(call())

    span_ids = [span.context.span_id for span in traced.spans()]
    assert len(span_ids) == 3
    assert seen_by_http == span_ids


def test_exa_span_is_a_child_of_the_active_span(fake):
    async def call(client) -> None:
        try:
            await client.answer("q")
        finally:
            await client.client.aclose()

    with instrumented() as traced:
        tracer = traced.provider.get_tracer("agent")
        with tracer.start_as_current_span("agent.step") as parent:
            Exa(api_key=EXA_KEY, base_url=fake.origin).search("q")
            asyncio.run(call(AsyncExa(api_key=EXA_KEY, api_base=fake.origin)))

    by_name = {span.name: span for span in traced.spans()}
    agent = by_name["agent.step"]
    assert agent.context.span_id == parent.get_span_context().span_id
    for name in ("exa.search", "exa.answer"):
        assert by_name[name].parent.span_id == agent.context.span_id
        assert by_name[name].context.trace_id == agent.context.trace_id


def test_exa_span_is_a_root_span_without_a_parent(fake):
    with instrumented() as traced:
        Exa(api_key=EXA_KEY, base_url=fake.origin).search("q")

    assert traced.one().parent is None
