"""AsyncParallel parity: the same calls give the same spans as Parallel."""

from __future__ import annotations

import asyncio

import pytest

pytest.importorskip("parallel", reason="parallel-web must be installed to test its instrumentor")

from opentelemetry.trace import StatusCode  # noqa: E402

from _parallel_support import (  # noqa: E402
    PARALLEL_KEY,
    USAGE,
    WARN,
    FakeParallel,
    async_client,
    attrs,
    instrumented,
    sync_client,
)

CALLS = [
    ("search", {"search_queries": ["parity " + PARALLEL_KEY, "two"], "mode": "basic"}),
    ("search", {"search_queries": [WARN + " " + USAGE], "session_id": "session_parity"}),
    ("extract", {"urls": ["https://x.example/a", "https://x.example/missing"]}),
    ("extract", {"urls": ["https://x.example/a"], "search_queries": ["focus"], "objective": "o"}),
]


@pytest.fixture()
def fake():
    with FakeParallel() as server:
        yield server


def _summary(span):
    return (
        span.name,
        attrs(span),
        span.status.status_code,
        [(event.name, dict(event.attributes)) for event in span.events],
    )


@pytest.mark.parametrize("options", [{}, {"capture_urls": True, "capture_objective": True}])
def test_async_spans_equal_sync_spans(fake, options):
    async def run_async() -> None:
        client = async_client(fake)
        try:
            for method, kwargs in CALLS:
                await getattr(client, method)(**kwargs)
        finally:
            await client.close()

    with instrumented(**options) as traced:
        client = sync_client(fake)
        for method, kwargs in CALLS:
            getattr(client, method)(**kwargs)
        sync_spans = traced.spans()
        traced.exporter.clear()
        asyncio.run(run_async())
        async_spans = traced.spans()

    assert len(sync_spans) == len(async_spans) == len(CALLS)
    assert [_summary(span) for span in async_spans] == [_summary(span) for span in sync_spans]
    assert all(span.status.status_code is StatusCode.OK for span in async_spans)
    # The same requests reached the fake from both clients.
    assert fake.paths() == [f"/v1/{method}" for method, _ in CALLS] * 2
    assert PARALLEL_KEY not in traced.wire()


def test_async_methods_still_return_coroutines(fake):
    import inspect

    from parallel import AsyncParallel

    async def call() -> None:
        client = async_client(fake)
        try:
            # Callers that inspect the method (tool registries) still see a
            # coroutine function on the class and on the bound method.
            assert inspect.iscoroutinefunction(client.search)
            pending = client.search(search_queries=["q"])
            assert asyncio.iscoroutine(pending)
            result = await pending
            assert len(result.results) == 2
        finally:
            await client.close()

    with instrumented() as traced:
        assert inspect.iscoroutinefunction(AsyncParallel.search)
        asyncio.run(call())

    assert traced.one().name == "parallel.search"
