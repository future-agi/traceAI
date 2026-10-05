"""Instrumentation failures never reach the caller.

Each test breaks one piece of the wrapper's own work (reading the client,
reading arguments, starting the span, making it current, reading the result,
recording the error, ending the span) and checks that the caller still gets
the vendor's own result or exception. Where the vendor path cannot produce
the fault, a wrapper helper or an SDK method is patched to raise.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

pytest.importorskip("tavily", reason="tavily-python must be installed to test its instrumentor")

from opentelemetry import trace as trace_api  # noqa: E402
from opentelemetry.sdk.trace import Span as SdkSpan  # noqa: E402
from opentelemetry.trace import StatusCode  # noqa: E402

from _support import attrs, instrumented  # noqa: E402
from _tavily_fake import FAIL_401, ODD_RESULTS, TAVILY_KEY, FakeTavily  # noqa: E402


@pytest.fixture()
def fake():
    with FakeTavily() as server:
        yield server


def _client(fake: FakeTavily) -> Any:
    from tavily import TavilyClient

    return TavilyClient(api_key=TAVILY_KEY, api_base_url=fake.origin)


async def _async_search(fake: FakeTavily, query: Any) -> Any:
    from tavily import AsyncTavilyClient

    client = AsyncTavilyClient(api_key=TAVILY_KEY, api_base_url=fake.origin)
    try:
        return await client.search(query)
    finally:
        await client.close()


class _Boom(RuntimeError):
    pass


def _boom(*_: Any, **__: Any) -> Any:
    raise _Boom("instrumentation failure")


class _ExplodingHeaders:
    def get(self, *_: Any) -> Any:
        raise _Boom("headers")


def test_an_unreadable_key_holder_drops_free_text_and_keeps_the_call(fake):
    """Fail closed: a key in a place that cannot be read could not be redacted."""
    from tavily import InvalidAPIKeyError

    with instrumented() as traced:
        client = _client(fake)
        # Not used by the SDK after __init__; the wrapper reads it for the key.
        client.headers = _ExplodingHeaders()
        result = client.search("q " + TAVILY_KEY)
        with pytest.raises(InvalidAPIKeyError):
            client.search(FAIL_401)

    assert len(result["results"]) == 2
    ok, failed = traced.spans()
    assert "input.value" not in attrs(ok)
    assert attrs(ok)["tavily.result_count"] == 2
    assert "input.value" not in attrs(failed)
    assert failed.status.description == "InvalidAPIKeyError"
    (event,) = [event for event in failed.events if event.name == "exception"]
    assert event.attributes["exception.message"] == "[redacted]"
    assert TAVILY_KEY not in traced.wire()


def test_an_unprintable_query_gets_the_vendors_own_error(fake):
    class Unprintable:
        def __str__(self) -> str:
            raise _Boom("str")

    with instrumented() as traced:
        with pytest.raises(TypeError, match="not JSON serializable"):
            _client(fake).search(Unprintable())

    span = traced.one()
    assert "input.value" not in attrs(span)
    assert span.status.status_code is StatusCode.ERROR
    assert span.status.description.startswith("TypeError")


@pytest.mark.parametrize("mode", ["sync", "async"])
def test_a_failing_tracer_leaves_the_call_untraced(fake, mode):
    class BrokenTracer(trace_api.Tracer):
        def start_span(self, *args: Any, **kwargs: Any) -> Any:
            raise _Boom("start_span")

        def start_as_current_span(self, *args: Any, **kwargs: Any) -> Any:
            raise _Boom("start_as_current_span")

    class BrokenProvider(trace_api.TracerProvider):
        def get_tracer(self, *args: Any, **kwargs: Any) -> trace_api.Tracer:
            return BrokenTracer()

    from traceai_tavily import TavilyInstrumentor

    instrumentor = TavilyInstrumentor()
    instrumentor.instrument(tracer_provider=BrokenProvider())
    try:
        if mode == "sync":
            result = _client(fake).search("still works")
        else:
            result = asyncio.run(_async_search(fake, "still works"))
    finally:
        instrumentor.uninstrument()

    assert len(result["results"]) == 2
    assert fake.paths() == ["/search"]


def test_a_failing_context_attach_does_not_reach_the_caller(fake, monkeypatch):
    import traceai_tavily._wrappers as wrappers

    monkeypatch.setattr(wrappers.context_api, "attach", _boom)
    with instrumented() as traced:
        result = _client(fake).search("q")

    assert len(result["results"]) == 2
    assert traced.one().status.status_code is StatusCode.OK


@pytest.mark.parametrize("mode", ["sync", "async"])
def test_an_unexpected_result_shape_omits_the_count(fake, mode):
    with instrumented() as traced:
        if mode == "sync":
            result = _client(fake).search(ODD_RESULTS)
        else:
            result = asyncio.run(_async_search(fake, ODD_RESULTS))

    assert result["results"] == {"unexpected": "shape"}
    span = traced.one()
    # Unknown, so absent: never 0.
    assert "tavily.result_count" not in attrs(span)
    assert span.status.status_code is StatusCode.OK


@pytest.mark.parametrize("mode", ["sync", "async"])
def test_a_failing_result_reader_still_returns_and_ends_the_span(fake, monkeypatch, mode):
    import traceai_tavily._wrappers as wrappers

    monkeypatch.setattr(wrappers, "_count", _boom)
    with instrumented() as traced:
        if mode == "sync":
            result = _client(fake).search("q")
        else:
            result = asyncio.run(_async_search(fake, "q"))

    assert len(result["results"]) == 2
    span = traced.one()
    assert "tavily.result_count" not in attrs(span)
    assert span.status.status_code is StatusCode.OK


def test_a_failing_exception_recorder_keeps_the_vendor_exception(fake, monkeypatch):
    from tavily import InvalidAPIKeyError

    monkeypatch.setattr(SdkSpan, "record_exception", _boom)
    with instrumented() as traced:
        with pytest.raises(InvalidAPIKeyError):
            _client(fake).search(FAIL_401)

    span = traced.one()
    assert span.status.status_code is StatusCode.ERROR


def test_an_unprintable_vendor_error_is_still_reraised(fake, monkeypatch):
    from tavily import InvalidAPIKeyError

    monkeypatch.setattr(InvalidAPIKeyError, "__str__", _boom)
    with instrumented() as traced:
        with pytest.raises(InvalidAPIKeyError):
            _client(fake).search(FAIL_401)

    span = traced.one()
    assert span.status.status_code is StatusCode.ERROR
    assert span.status.description == "InvalidAPIKeyError"


def test_a_failing_span_end_does_not_reach_the_caller(fake, monkeypatch):
    from tavily import InvalidAPIKeyError

    monkeypatch.setattr(SdkSpan, "end", _boom)
    with instrumented():
        assert len(_client(fake).search("q")["results"]) == 2
        with pytest.raises(InvalidAPIKeyError):
            _client(fake).search(FAIL_401)
