"""Errors, timeouts and cancellation: recorded on the span, never changed for the caller."""

from __future__ import annotations

import asyncio

import pytest

pytest.importorskip("voyageai", reason="voyageai must be installed to test its instrumentor")

import voyageai  # noqa: E402
from voyageai.api_resources import api_requestor  # noqa: E402

from _support import (  # noqa: E402
    DOCUMENTS,
    EMBED_MODEL,
    QUERY,
    TEXTS,
    VOYAGE_KEY,
    FakeVoyage,
    async_client,
    attrs,
    client,
    event_names,
    instrumented,
    status_code,
)

_RESULT_KEYS = (
    "gen_ai.usage.input_tokens",
    "gen_ai.usage.total_tokens",
    "voyage.embedding.dimension",
    "voyage.rerank.result_count",
    "voyage.cancelled",
)


@pytest.fixture()
def fake():
    with FakeVoyage() as server:
        yield server


def test_an_api_error_sets_error_status_and_one_exception_event(fake):
    with instrumented() as traced:
        with pytest.raises(voyageai.error.AuthenticationError):
            client(fake).embed(TEXTS, model="fail-401")
        with pytest.raises(voyageai.error.AuthenticationError):
            client(fake).rerank(QUERY, DOCUMENTS, model="fail-401")

    for span in traced.spans():
        assert status_code(span) == "ERROR"
        assert span.status.description.startswith("AuthenticationError: ")
        assert event_names(span) == ["exception"]
        (event,) = span.events
        assert event.attributes["exception.type"].endswith("AuthenticationError")
        values = attrs(span)
        # Nothing came back, so result counts and usage are absent, not 0.
        for key in _RESULT_KEYS:
            assert key not in values, key
    embed, rerank = traced.spans()
    assert attrs(embed)["voyage.embedding.count"] == 2
    assert attrs(rerank)["voyage.rerank.document_count"] == 3


def test_a_key_echoed_in_an_error_message_is_redacted(fake):
    with instrumented() as traced:
        with pytest.raises(voyageai.error.InvalidRequestError) as raised:
            client(fake).embed(TEXTS, model="echo-key")

    # The caller still sees the vendor's message unchanged.
    assert VOYAGE_KEY in str(raised.value)
    span = traced.one()
    assert "[redacted]" in span.status.description
    assert "[redacted]" in span.events[0].attributes["exception.message"]
    assert VOYAGE_KEY not in traced.wire()


def test_a_client_timeout_ends_the_span_as_an_error(fake):
    with instrumented() as traced:
        with pytest.raises(voyageai.error.Timeout):
            client(fake, timeout=0.3).embed(TEXTS, model="slow")

    span = traced.one()
    assert status_code(span) == "ERROR"
    assert span.status.description.startswith("Timeout")
    assert "voyage.cancelled" not in attrs(span)


def test_cancelling_an_async_call_marks_the_span_cancelled(fake):
    async def cancel_mid_call() -> None:
        task = asyncio.ensure_future(async_client(fake).embed(TEXTS, model="slow"))
        while not fake.received.is_set():
            await asyncio.sleep(0.01)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    with instrumented() as traced:
        asyncio.run(cancel_mid_call())

    span = traced.one()
    assert status_code(span) == "ERROR"
    assert span.status.description == "cancelled"
    assert attrs(span)["voyage.cancelled"] is True
    # Cancellation is not an exception event, and nothing came back.
    assert event_names(span) == []
    assert "gen_ai.usage.total_tokens" not in attrs(span)


def test_a_keyboard_interrupt_during_a_sync_call_marks_the_span_cancelled(fake, monkeypatch):
    def interrupted(self, *args, **kwargs):
        raise KeyboardInterrupt

    monkeypatch.setattr(api_requestor.APIRequestor, "request_raw", interrupted)
    with instrumented() as traced:
        with pytest.raises(KeyboardInterrupt):
            client(fake).rerank(QUERY, DOCUMENTS, model="rerank-2.5")

    span = traced.one()
    assert span.status.description == "cancelled"
    assert attrs(span)["voyage.cancelled"] is True
    assert event_names(span) == []


class _Unprintable(Exception):
    def __str__(self) -> str:
        raise RuntimeError("str() is broken")


def test_the_vendor_exception_is_reraised_unchanged_even_if_unprintable(fake, monkeypatch):
    raised = _Unprintable()

    def broken(self, *args, **kwargs):
        raise raised

    monkeypatch.setattr(api_requestor.APIRequestor, "request_raw", broken)
    with instrumented() as traced:
        with pytest.raises(_Unprintable) as caught:
            client(fake).embed(TEXTS, model=EMBED_MODEL)

    assert caught.value is raised
    span = traced.one()
    assert status_code(span) == "ERROR"
    assert span.end_time is not None


def test_a_failing_request_attribute_never_reaches_the_caller(fake, monkeypatch):
    import traceai_voyage._wrappers as wrappers

    def explode(*args, **kwargs):
        raise RuntimeError("attribute extraction failed")

    monkeypatch.setattr(wrappers, "_request_attributes", explode)
    with instrumented() as traced:
        result = client(fake).embed(TEXTS, model=EMBED_MODEL)

    assert len(result.embeddings) == 2
    values = attrs(traced.one())
    assert values["gen_ai.span.kind"] == "EMBEDDING"
    assert values["gen_ai.usage.total_tokens"] == 14


def test_a_failing_result_attribute_never_reaches_the_caller(fake, monkeypatch):
    import traceai_voyage._wrappers as wrappers

    def explode(*args, **kwargs):
        raise RuntimeError("result extraction failed")

    monkeypatch.setattr(wrappers, "_response_attributes", explode)
    with instrumented() as traced:
        result = client(fake).rerank(QUERY, DOCUMENTS, model="rerank-2.5")
        async_result = asyncio.run(
            async_client(fake).rerank(QUERY, DOCUMENTS, model="rerank-2.5")
        )

    assert [r.index for r in result.results] == [1, 0, 2]
    assert [r.index for r in async_result.results] == [1, 0, 2]
    for span in traced.spans():
        assert span.end_time is not None
        assert status_code(span) == "OK"


def test_a_tracer_that_raises_never_breaks_the_vendor_call(fake):
    from opentelemetry import trace as trace_api

    from traceai_voyage import VoyageInstrumentor

    class ExplodingTracer(trace_api.Tracer):
        def start_span(self, *args, **kwargs):
            raise RuntimeError("tracer is broken")

        def start_as_current_span(self, *args, **kwargs):
            raise RuntimeError("tracer is broken")

    class ExplodingProvider(trace_api.TracerProvider):
        def get_tracer(self, *args, **kwargs):
            return ExplodingTracer()

    instrumentor = VoyageInstrumentor()
    instrumentor.instrument(tracer_provider=ExplodingProvider())
    try:
        result = client(fake).embed(TEXTS, model=EMBED_MODEL)
        async_result = asyncio.run(async_client(fake).embed(TEXTS, model=EMBED_MODEL))
    finally:
        instrumentor.uninstrument()

    assert len(result.embeddings) == 2
    assert len(async_result.embeddings) == 2
