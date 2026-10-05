"""AsyncExa parity: the async twins give the same spans as Exa (AC-05)."""

from __future__ import annotations

import asyncio
import warnings

import pytest
from opentelemetry.trace import StatusCode

pytest.importorskip("exa_py", reason="exa-py must be installed to test its instrumentor")

from _support import CONTENT_MARKERS, EXA_KEY, FAIL_QUERY, FakeExa, attrs, instrumented  # noqa: E402
from exa_py import AsyncExa, Exa  # noqa: E402

URLS = ["https://example.com/0", "https://example.com/1"]


@pytest.fixture()
def fake():
    with FakeExa() as server:
        yield server


def _summary(traced):
    return [
        (span.name, span.status.status_code, sorted(attrs(span).items()))
        for span in traced.spans()
    ]


def _sync_calls(client: Exa) -> None:
    client.search("parity query", num_results=2)
    client.get_contents(URLS)
    client.answer("parity query")
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", DeprecationWarning)
        client.search_and_contents("parity query")


async def _async_calls(client: AsyncExa) -> None:
    try:
        await client.search("parity query", num_results=2)
        await client.get_contents(URLS)
        await client.answer("parity query")
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", DeprecationWarning)
            await client.search_and_contents("parity query")
    finally:
        await client.client.aclose()


def test_async_client_produces_the_same_spans_as_the_sync_client(fake):
    with instrumented() as sync_traced:
        _sync_calls(Exa(api_key=EXA_KEY, base_url=fake.origin))
    with instrumented() as async_traced:
        asyncio.run(_async_calls(AsyncExa(api_key=EXA_KEY, api_base=fake.origin)))

    expected = _summary(sync_traced)
    assert [name for name, _, _ in expected] == [
        "exa.search",
        "exa.get_contents",
        "exa.answer",
        "exa.search",
    ]
    assert all(status is StatusCode.OK for _, status, _ in expected)
    assert _summary(async_traced) == expected
    # Both clients made real HTTP calls to the fake.
    assert fake.paths() == ["/search", "/contents", "/answer", "/search"] * 2
    for traced in (sync_traced, async_traced):
        wire = traced.wire()
        assert EXA_KEY not in wire
        for marker in CONTENT_MARKERS:
            assert marker not in wire


def test_async_vendor_error_is_recorded_and_reraised(fake):
    async def call() -> None:
        client = AsyncExa(api_key=EXA_KEY, api_base=fake.origin)
        try:
            with pytest.raises(ValueError, match="401"):
                await client.search(FAIL_QUERY)
        finally:
            await client.client.aclose()

    with instrumented() as traced:
        asyncio.run(call())

    span = traced.one()
    assert span.status.status_code is StatusCode.ERROR
    assert [event.name for event in span.events] == ["exception"]
    assert "exa.cancelled" not in attrs(span)
    assert "fi.retrieval.document_count" not in attrs(span)
