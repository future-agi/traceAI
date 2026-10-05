"""Errors, cancellation and the missing-key case, sync and async."""

from __future__ import annotations

import asyncio

import pytest

pytest.importorskip("parallel", reason="parallel-web must be installed to test its instrumentor")

import parallel  # noqa: E402
from opentelemetry.trace import StatusCode  # noqa: E402

from _parallel_support import (  # noqa: E402
    FAIL_401,
    FAIL_500,
    PARALLEL_KEY,
    SLOW,
    FakeParallel,
    async_client,
    attrs,
    instrumented,
    sync_client,
)


@pytest.fixture()
def fake():
    with FakeParallel() as server:
        yield server


def _exception_event(span):
    events = [event for event in span.events if event.name == "exception"]
    assert len(events) == 1, [event.name for event in span.events]
    return dict(events[0].attributes)


def test_http_401_sets_error_reraises_and_never_records_the_key(fake):
    with instrumented() as traced:
        with pytest.raises(parallel.AuthenticationError) as raised:
            sync_client(fake).search(search_queries=[FAIL_401])

    # The vendor's exception reaches the caller unchanged (it still names the
    # key the server echoed); only the span copy is redacted.
    assert PARALLEL_KEY in str(raised.value)
    span = traced.one()
    assert span.status.status_code is StatusCode.ERROR
    assert span.status.description.startswith("AuthenticationError: Error code: 401")
    assert "[redacted]" in span.status.description
    event = _exception_event(span)
    assert event["exception.type"] == "parallel.AuthenticationError"
    assert "[redacted]" in event["exception.message"]
    assert "Traceback" in event["exception.stacktrace"]
    assert PARALLEL_KEY not in traced.wire()
    # Nothing came back, so no result count or ids are recorded.
    for key in ("parallel.result_count", "parallel.search_id"):
        assert key not in attrs(span)


def test_http_500_on_extract_sets_error_and_reraises(fake):
    with instrumented() as traced:
        with pytest.raises(parallel.InternalServerError):
            sync_client(fake).extract(urls=["https://x.example/" + FAIL_500])

    span = traced.one()
    assert span.name == "parallel.extract"
    assert span.status.status_code is StatusCode.ERROR
    assert span.status.description.startswith("InternalServerError: Error code: 500")
    assert _exception_event(span)["exception.type"] == "parallel.InternalServerError"


def test_connection_error_sets_error_and_reraises():
    from parallel import Parallel

    with FakeParallel() as fake:
        origin = fake.origin
    # The fake is closed: nothing listens on its port any more.
    with instrumented() as traced:
        with pytest.raises(parallel.APIConnectionError):
            Parallel(api_key=PARALLEL_KEY, base_url=origin, max_retries=0).search(
                search_queries=["q"]
            )

    span = traced.one()
    assert span.status.status_code is StatusCode.ERROR
    assert "parallel.cancelled" not in attrs(span)


def test_async_http_401_matches_the_sync_span(fake):
    async def call() -> None:
        client = async_client(fake)
        try:
            with pytest.raises(parallel.AuthenticationError):
                await client.search(search_queries=[FAIL_401])
        finally:
            await client.close()

    with instrumented() as traced:
        with pytest.raises(parallel.AuthenticationError):
            sync_client(fake).search(search_queries=[FAIL_401])
        asyncio.run(call())

    sync_span, async_span = traced.spans()
    assert attrs(sync_span) == attrs(async_span)
    assert sync_span.status.status_code is async_span.status.status_code is StatusCode.ERROR
    assert sync_span.status.description == async_span.status.description
    assert PARALLEL_KEY not in traced.wire()


@pytest.mark.parametrize("operation", ["search", "extract"])
def test_cancelling_an_async_call_marks_the_span_cancelled(fake, operation):
    async def call() -> None:
        client = async_client(fake)
        try:
            if operation == "search":
                request = client.search(search_queries=[SLOW])
            else:
                request = client.extract(urls=["https://x.example/" + SLOW])
            task = asyncio.ensure_future(request)
            for _ in range(200):
                if fake.received.is_set():
                    break
                await asyncio.sleep(0.01)
            assert fake.received.is_set()
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
        finally:
            await client.close()

    with instrumented() as traced:
        asyncio.run(call())

    span = traced.one()
    assert span.name == "parallel.{0}".format(operation)
    assert span.status.status_code is StatusCode.ERROR
    assert span.status.description == "cancelled"
    assert attrs(span)["parallel.cancelled"] is True
    # Cancellation is not an exception: no event, no result count.
    assert span.events == ()
    assert "parallel.result_count" not in attrs(span)


def test_missing_api_key_raises_in_the_constructor_before_any_span(monkeypatch):
    # Parallel()/AsyncParallel() raise in __init__, which is not a traced call.
    from parallel import AsyncParallel, Parallel

    monkeypatch.delenv("PARALLEL_API_KEY", raising=False)
    with instrumented() as traced:
        with pytest.raises(parallel.ParallelError, match="api_key"):
            Parallel()
        with pytest.raises(parallel.ParallelError, match="api_key"):
            AsyncParallel()

    assert traced.spans() == []
