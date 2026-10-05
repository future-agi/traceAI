"""Instrumentation failures never reach the caller; suppression and nesting guards hold."""

from __future__ import annotations

import asyncio

import pytest
from google.api_core import exceptions as core_exceptions
from opentelemetry.trace import StatusCode

from _discoveryengine_support import (
    ANSWER_REFERENCES,
    FAIL_DENIED,
    SEARCH_RESULTS,
    FakeDiscoveryEngine,
    answer_client,
    answer_request,
    async_answer_client,
    attrs,
    instrumented,
    search_client,
    search_request,
)
from traceai_discoveryengine import DiscoveryEngineInstrumentor, _wrappers


@pytest.fixture()
def fake():
    with FakeDiscoveryEngine() as server:
        yield server


def _boom(*_args, **_kwargs):
    raise RuntimeError("instrumentation bug")


def test_a_failing_request_reader_still_returns_the_result(fake, monkeypatch):
    monkeypatch.setattr(_wrappers, "_request_attributes", _boom)
    with instrumented(capture_query=True) as traced:
        pager = search_client(fake).search(request=search_request("q"))

    assert len(pager.results) == SEARCH_RESULTS
    span = traced.one()
    assert attrs(span)["fi.span.kind"] == "RETRIEVER"
    assert span.status.status_code is StatusCode.OK


def test_a_failing_response_reader_still_returns_the_result_and_ends_the_span(fake, monkeypatch):
    monkeypatch.setattr(_wrappers, "_response_attributes", _boom)

    async def call():
        client = async_answer_client(fake)
        try:
            return await client.answer_query(request=answer_request())
        finally:
            await client.transport.close()

    with instrumented() as traced:
        pager = search_client(fake).search(request=search_request())
        response = asyncio.run(call())

    assert len(pager.results) == SEARCH_RESULTS
    assert len(response.answer.references) == ANSWER_REFERENCES
    spans = traced.spans()
    assert [span.name for span in spans] == ["discoveryengine.search", "discoveryengine.answer_query"]
    assert all(span.end_time is not None for span in spans)
    assert all(span.status.status_code is StatusCode.OK for span in spans)


def test_a_failing_error_recorder_still_raises_the_vendor_error(fake, monkeypatch):
    monkeypatch.setattr(_wrappers, "_exception_attributes", _boom)
    monkeypatch.setattr(_wrappers, "_error_attributes", _boom)
    with instrumented() as traced:
        with pytest.raises(core_exceptions.PermissionDenied):
            search_client(fake).search(request=search_request(FAIL_DENIED))

    span = traced.one()
    assert span.end_time is not None
    assert span.status.status_code is StatusCode.ERROR
    assert span.events == ()


def test_a_failing_credential_lookup_still_runs_the_call(fake, monkeypatch):
    monkeypatch.setattr(_wrappers, "_credential_values", _boom)
    with instrumented(capture_query=True) as traced:
        pager = search_client(fake).search(request=search_request("q"))

    assert len(pager.results) == SEARCH_RESULTS
    # Fail closed: the query is not recorded when credentials could not be read.
    values = attrs(traced.one())
    assert "input.value" not in values
    assert values["discoveryengine.result_count"] == SEARCH_RESULTS


class _BrokenTracer:
    def start_span(self, *_args, **_kwargs):
        raise RuntimeError("tracer bug")


class _BrokenProvider:
    def get_tracer(self, *_args, **_kwargs):
        return _BrokenTracer()


def test_a_tracer_that_cannot_start_spans_leaves_calls_untraced(fake):
    instrumentor = DiscoveryEngineInstrumentor()
    instrumentor.instrument(tracer_provider=_BrokenProvider())
    try:
        pager = search_client(fake).search(request=search_request())

        async def call():
            client = async_answer_client(fake)
            try:
                return await client.answer_query(request=answer_request())
            finally:
                await client.transport.close()

        response = asyncio.run(call())
    finally:
        instrumentor.uninstrument()

    assert len(pager.results) == SEARCH_RESULTS
    assert len(response.answer.references) == ANSWER_REFERENCES


def test_suppress_tracing_skips_the_span(fake):
    from fi_instrumentation import suppress_tracing

    with instrumented() as traced:
        with suppress_tracing():
            search_client(fake).search(request=search_request())
        answer_client(fake).answer_query(request=answer_request())

    assert fake.methods() == ["Search", "AnswerQuery"]
    assert traced.names() == ["discoveryengine.answer_query"]


def test_a_wrapped_call_made_inside_a_traced_call_is_not_a_second_span(fake, monkeypatch):
    # No 0.20.5 method calls another wrapped one, but a later release could
    # (an async client delegating to its sync client). The guard keeps one
    # span per user call.
    from google.cloud.discoveryengine_v1 import SearchServiceClient

    with instrumented() as traced:
        client = search_client(fake)
        inner = client.search_lite
        original = client._transport._wrapped_methods[client._transport.search]

        def nested(request, **kwargs):
            inner(request=search_request())
            return original(request, **kwargs)

        monkeypatch.setitem(client._transport._wrapped_methods, client._transport.search, nested)
        client.search(request=search_request())
        assert isinstance(client, SearchServiceClient)

    assert fake.methods() == ["SearchLite", "Search"]
    assert traced.names() == ["discoveryengine.search"]
