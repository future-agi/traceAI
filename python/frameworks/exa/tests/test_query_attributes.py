"""Request attributes from the real SDK against the loopback fake."""

from __future__ import annotations

import asyncio

import pytest

pytest.importorskip("exa_py", reason="exa-py must be installed to test its instrumentor")

from _support import EXA_KEY, FakeExa, attrs, instrumented  # noqa: E402
from exa_py import AsyncExa, Exa  # noqa: E402


@pytest.fixture()
def fake():
    with FakeExa() as server:
        yield server


def test_api_key_embedded_in_query_is_redacted(fake):
    query = "find {0} please".format(EXA_KEY)
    with instrumented() as traced:
        Exa(api_key=EXA_KEY, base_url=fake.origin).search(query)

    assert attrs(traced.one())["fi.retrieval.query"] == "find [redacted] please"
    assert EXA_KEY not in traced.wire()
    # Redaction is on the span only; the vendor still sent the caller's query.
    assert fake.calls[0][2]["query"] == query


def test_api_key_embedded_in_async_query_is_redacted(fake):
    async def call() -> None:
        client = AsyncExa(api_key=EXA_KEY, api_base=fake.origin)
        try:
            await client.search("async {0}".format(EXA_KEY))
        finally:
            await client.client.aclose()

    with instrumented() as traced:
        asyncio.run(call())

    assert attrs(traced.one())["fi.retrieval.query"] == "async [redacted]"
    assert EXA_KEY not in traced.wire()
