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


@pytest.mark.parametrize(
    "query",
    [
        "a" * 2000,
        "\u20ac" * 500,  # 3 UTF-8 bytes each: 1024 is not a character boundary
        "\U0001f50d" * 300,  # 4 UTF-8 bytes each
        "x" + "\u00e9" * 600,  # 2-byte characters after an odd offset
    ],
    ids=["ascii", "3-byte", "4-byte", "2-byte-offset"],
)
def test_query_is_capped_at_1kb_of_utf8_on_a_character_boundary(fake, query):
    with instrumented() as traced:
        Exa(api_key=EXA_KEY, base_url=fake.origin).search(query)

    recorded = attrs(traced.one())["fi.retrieval.query"]
    assert len(recorded.encode("utf-8")) <= 1024
    # The longest whole-character prefix that fits: one more character would not.
    assert recorded == query[: len(recorded)]
    assert len(query[: len(recorded) + 1].encode("utf-8")) > 1024
