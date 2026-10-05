"""Instrumentation failures never reach the caller; suppression and nesting guards hold."""

from __future__ import annotations

import asyncio
import contextlib

import pytest
from google.api_core import exceptions as core_exceptions
from opentelemetry.trace import StatusCode

from _discoveryengine_support import (
    ANSWER_REFERENCES,
    FAIL_DENIED,
    METADATA_TOKEN,
    SEARCH_RESULTS,
    FakeDiscoveryEngine,
    answer_client,
    answer_request,
    async_answer_client,
    async_search_client,
    attrs,
    event,
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


def test_a_query_that_cannot_be_read_keeps_no_server_text(fake, monkeypatch):
    # The query is hidden by default; if it cannot be read, the server's
    # error text (which may echo it) is replaced as a whole.
    from fi_instrumentation import REDACTED_VALUE

    monkeypatch.setattr(_wrappers, "_hidden_inputs", _boom)
    with instrumented() as traced:
        with pytest.raises(core_exceptions.PermissionDenied):
            search_client(fake).search(request=search_request(FAIL_DENIED))

    span = traced.one()
    assert span.status.description == "PermissionDenied: " + REDACTED_VALUE
    exception = [item for item in span.events if item.name == "exception"][0].attributes
    assert exception["exception.message"] == REDACTED_VALUE
    assert exception["exception.stacktrace"] == REDACTED_VALUE
    assert attrs(span)["discoveryengine.error.status"] == "PERMISSION_DENIED"


def test_a_failing_credential_lookup_still_runs_the_call(fake, monkeypatch):
    monkeypatch.setattr(_wrappers, "_credential_values", _boom)
    with instrumented(capture_query=True) as traced:
        pager = search_client(fake).search(request=search_request("q"))

    assert len(pager.results) == SEARCH_RESULTS
    # Fail closed: the query is not recorded when credentials could not be read.
    values = attrs(traced.one())
    assert "input.value" not in values
    assert values["discoveryengine.result_count"] == SEARCH_RESULTS


def _metadata_pairs():
    # A one-shot iterable: a generator can be read once only.
    yield ("x-caller-header", "caller-value")
    yield ("x-goog-api-key", METADATA_TOKEN)


@pytest.mark.parametrize("traced", [False, True], ids=["uninstrumented", "instrumented"])
def test_metadata_from_a_generator_reaches_the_server(fake, traced):
    async def call():
        client = async_search_client(fake)
        try:
            await client.search_lite(request=search_request(), metadata=_metadata_pairs())
        finally:
            await client.transport.close()

    with instrumented() if traced else contextlib.nullcontext() as tracing:
        with pytest.raises(core_exceptions.PermissionDenied):
            search_client(fake).search(request=search_request(FAIL_DENIED), metadata=_metadata_pairs())
        answer_client(fake).answer_query(request=answer_request(), metadata=_metadata_pairs())
        asyncio.run(call())

    assert fake.methods() == ["Search", "AnswerQuery", "SearchLite"]
    for received in fake.calls:
        assert received.metadata["x-caller-header"] == "caller-value"
        assert received.metadata["x-goog-api-key"] == METADATA_TOKEN
    if traced:
        assert len(tracing.spans()) == 3
        # The pairs read once also feed the credential lookup.
        assert METADATA_TOKEN not in tracing.wire()


def _failing_metadata_pairs():
    yield ("x-caller-header", "caller-value")
    raise RuntimeError("caller metadata failed")


@pytest.mark.parametrize("traced", [False, True], ids=["uninstrumented", "instrumented"])
def test_metadata_that_fails_while_read_raises_as_without_instrumentation(fake, traced):
    with instrumented() if traced else contextlib.nullcontext() as tracing:
        with pytest.raises(RuntimeError, match="caller metadata failed"):
            search_client(fake).search(request=search_request(), metadata=_failing_metadata_pairs())

    # The client raised before sending anything, as it does uninstrumented.
    assert fake.calls == []
    if traced:
        span = tracing.one()
        # Nothing from metadata that could not be read: no free text at all.
        assert span.status.description == "RuntimeError: " + _wrappers.UNREADABLE
        assert event(span, "exception")["exception.message"] == _wrappers.UNREADABLE


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
