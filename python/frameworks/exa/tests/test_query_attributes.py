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


def test_input_value_carries_the_same_redacted_capped_query(fake):
    query = "{0} {1}".format(EXA_KEY, "\u20ac" * 500)
    with instrumented() as traced:
        client = Exa(api_key=EXA_KEY, base_url=fake.origin)
        client.search(query)
        client.answer(query)
        list(client.stream_search(query))

    spans = traced.spans()
    assert [span.name for span in spans] == ["exa.search", "exa.answer", "exa.search"]
    for span in spans:
        values = attrs(span)
        assert values["input.value"] == values["fi.retrieval.query"]
        assert values["input.value"].startswith("[redacted] \u20ac")
        assert len(values["input.value"].encode("utf-8")) <= 1024


SECRET_URL = "https://x.example/a?token=URL-SECRET"


def test_get_contents_records_the_url_count_not_the_urls(fake):
    with instrumented() as traced:
        response = Exa(api_key=EXA_KEY, base_url=fake.origin).get_contents(
            [SECRET_URL, "https://x.example/b"]
        )

    assert len(response.results) == 2
    span = traced.one()
    values = attrs(span)
    assert span.name == "exa.get_contents"
    assert values["fi.retrieval.url_count"] == 2
    assert values["fi.retrieval.document_count"] == 2
    assert "fi.retrieval.urls" not in values
    assert "fi.retrieval.query" not in values
    assert "input.value" not in values
    for secret in ("URL-SECRET", "x.example"):
        assert secret not in traced.wire()


def test_get_contents_counts_every_input_shape(fake):
    with instrumented() as traced:
        client = Exa(api_key=EXA_KEY, base_url=fake.origin)
        found = client.search("documents to fetch")  # two Result objects
        client.get_contents("https://x.example/one")
        client.get_contents(urls=["https://x.example/1", "https://x.example/2", "https://x.example/3"])
        client.get_contents(found.results)

    counts = [
        attrs(span).get("fi.retrieval.url_count")
        for span in traced.spans()
        if span.name == "exa.get_contents"
    ]
    assert counts == [1, 3, 2]
    assert "x.example" not in traced.wire()
    assert "https://example.com/" not in traced.wire()


def test_async_get_contents_records_the_url_count_not_the_urls(fake):
    async def call() -> None:
        client = AsyncExa(api_key=EXA_KEY, api_base=fake.origin)
        try:
            await client.get_contents([SECRET_URL])
        finally:
            await client.client.aclose()

    with instrumented() as traced:
        asyncio.run(call())

    assert attrs(traced.one())["fi.retrieval.url_count"] == 1
    assert "URL-SECRET" not in traced.wire()


def test_capture_urls_opt_in_records_at_most_20_redacted_capped_urls(fake):
    urls = ["https://x.example/{0}".format(i) for i in range(25)]
    urls[0] = "https://x.example/k?key={0}".format(EXA_KEY)
    urls[1] = "https://x.example/" + "\u20ac" * 500
    with instrumented(capture_urls=True) as traced:
        Exa(api_key=EXA_KEY, base_url=fake.origin).get_contents(urls)

    values = attrs(traced.one())
    assert values["fi.retrieval.url_count"] == 25
    captured = list(values["fi.retrieval.urls"])
    assert len(captured) == 20
    assert captured[0] == "https://x.example/k?key=[redacted]"
    assert all(len(url.encode("utf-8")) <= 1024 for url in captured)
    assert captured[1] == urls[1][: len(captured[1])]
    assert captured[2:] == urls[2:20]
    assert EXA_KEY not in traced.wire()


def test_capture_urls_does_not_add_urls_to_search_spans(fake):
    with instrumented(capture_urls=True) as traced:
        Exa(api_key=EXA_KEY, base_url=fake.origin).search("plain search")

    assert "fi.retrieval.urls" not in attrs(traced.one())
