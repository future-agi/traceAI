"""Instrumentation failures never reach the caller; suppression is honoured."""

from __future__ import annotations

import asyncio

import pytest

pytest.importorskip("parallel", reason="parallel-web must be installed to test its instrumentor")

import parallel  # noqa: E402
from opentelemetry.trace import StatusCode  # noqa: E402

from _parallel_support import (  # noqa: E402
    FAIL_401,
    FakeParallel,
    async_client,
    attrs,
    instrumented,
    sync_client,
)
from traceai_parallel import ParallelInstrumentor, _wrappers  # noqa: E402


@pytest.fixture()
def fake():
    with FakeParallel() as server:
        yield server


def _boom(*_args, **_kwargs):
    raise RuntimeError("instrumentation bug")


def test_a_failing_request_attribute_builder_still_returns_the_result(fake, monkeypatch):
    monkeypatch.setattr(_wrappers, "_request_attributes", _boom)
    with instrumented() as traced:
        result = sync_client(fake).search(search_queries=["q"])

    assert len(result.results) == 2
    span = traced.one()
    assert attrs(span)["fi.span.kind"] == "RETRIEVER"
    assert span.status.status_code is StatusCode.OK


def test_a_failing_response_reader_still_returns_the_result_and_ends_the_span(fake, monkeypatch):
    monkeypatch.setattr(_wrappers, "_response_attributes", _boom)

    async def call():
        client = async_client(fake)
        try:
            return await client.extract(urls=["https://x.example/a"])
        finally:
            await client.close()

    with instrumented() as traced:
        result = sync_client(fake).search(search_queries=["q"])
        async_result = asyncio.run(call())

    assert len(result.results) == 2
    assert len(async_result.results) == 1
    spans = traced.spans()
    assert [span.name for span in spans] == ["parallel.search", "parallel.extract"]
    assert all(span.end_time is not None for span in spans)
    assert all(span.status.status_code is StatusCode.OK for span in spans)


def test_a_failing_error_recorder_still_raises_the_vendor_error(fake, monkeypatch):
    monkeypatch.setattr(_wrappers, "_exception_attributes", _boom)
    with instrumented() as traced:
        with pytest.raises(parallel.AuthenticationError):
            sync_client(fake).search(search_queries=[FAIL_401])

    span = traced.one()
    assert span.end_time is not None
    assert span.status.status_code is StatusCode.ERROR
    assert span.events == ()


def test_an_exception_whose_str_raises_is_recorded_without_breaking_the_call():
    class Unprintable(Exception):
        def __str__(self) -> str:
            raise ValueError("no str for you")

    error = Unprintable()

    def wrapped(**_kwargs):
        raise error

    from _parallel_support import new_provider

    exporter, provider = new_provider()
    wrapper = _wrappers.OperationWrapper(
        provider.get_tracer("t"), "search", _wrappers.Options()
    )
    with pytest.raises(Unprintable) as raised:
        wrapper(wrapped, object(), (), {"search_queries": ["q"]})

    assert raised.value is error
    (span,) = exporter.get_finished_spans()
    assert span.status.status_code is StatusCode.ERROR
    assert span.status.description.startswith("Unprintable")


class _BrokenTracer:
    def start_span(self, *_args, **_kwargs):
        raise RuntimeError("tracer bug")


class _BrokenProvider:
    def get_tracer(self, *_args, **_kwargs):
        return _BrokenTracer()


def test_a_tracer_that_cannot_start_spans_leaves_calls_untraced(fake):
    instrumentor = ParallelInstrumentor()
    instrumentor.instrument(tracer_provider=_BrokenProvider())
    try:
        result = sync_client(fake).search(search_queries=["q"])

        async def call():
            client = async_client(fake)
            try:
                return await client.extract(urls=["https://x.example/a"])
            finally:
                await client.close()

        async_result = asyncio.run(call())
    finally:
        instrumentor.uninstrument()

    assert len(result.results) == 2
    assert len(async_result.results) == 1


def test_suppress_tracing_skips_the_span(fake):
    from fi_instrumentation import suppress_tracing

    with instrumented() as traced:
        with suppress_tracing():
            sync_client(fake).search(search_queries=["q"])
        sync_client(fake).search(search_queries=["q"])

    assert len(fake.calls) == 2
    assert [span.name for span in traced.spans()] == ["parallel.search"]
