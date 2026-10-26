"""uninstrument() restores every wrapped method; construction order does not matter."""

from __future__ import annotations

import asyncio

import pytest

pytest.importorskip("voyageai", reason="voyageai must be installed to test its instrumentor")

import voyageai  # noqa: E402
from opentelemetry.sdk.trace import TracerProvider  # noqa: E402
from opentelemetry.sdk.trace.export import SimpleSpanProcessor  # noqa: E402
from opentelemetry.sdk.trace.export.in_memory_span_exporter import (  # noqa: E402
    InMemorySpanExporter,
)
from traceai_voyage import VoyageInstrumentor  # noqa: E402

from _support import (  # noqa: E402
    DOCUMENTS,
    EMBED_MODEL,
    QUERY,
    RERANK_MODEL,
    TEXTS,
    FakeVoyage,
    async_client,
    client,
    instrumented,
)

_WRAPPED = [
    (voyageai.Client, "embed"),
    (voyageai.Client, "rerank"),
    (voyageai.AsyncClient, "embed"),
    (voyageai.AsyncClient, "rerank"),
]


@pytest.fixture()
def fake():
    with FakeVoyage() as server:
        yield server


def _all_calls(fake) -> None:
    voyage = client(fake)
    voyage.embed(TEXTS, model=EMBED_MODEL)
    voyage.rerank(QUERY, DOCUMENTS, model=RERANK_MODEL)

    async def calls() -> None:
        avoyage = async_client(fake)
        await avoyage.embed(TEXTS, model=EMBED_MODEL)
        await avoyage.rerank(QUERY, DOCUMENTS, model=RERANK_MODEL)

    asyncio.run(calls())


def test_instrument_wraps_exactly_four_methods_and_uninstrument_restores_them(fake):
    originals = {(cls, name): cls.__dict__[name] for cls, name in _WRAPPED}
    with instrumented() as traced:
        for cls, name in _WRAPPED:
            assert hasattr(cls.__dict__[name], "__wrapped__"), (cls, name)
        _all_calls(fake)
        assert [span.name for span in traced.spans()] == [
            "voyage.embed",
            "voyage.rerank",
            "voyage.embed",
            "voyage.rerank",
        ]

    for (cls, name), original in originals.items():
        assert cls.__dict__[name] is original, (cls, name)

    # No spans once uninstrumented.
    _all_calls(fake)
    assert len(traced.spans()) == 4


def test_instrument_again_after_uninstrument(fake):
    with instrumented():
        pass
    with instrumented() as traced:
        client(fake).embed(TEXTS, model=EMBED_MODEL)
    assert len(traced.spans()) == 1


def test_a_second_instrument_call_does_not_double_wrap(fake):
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    instrumentor = VoyageInstrumentor()
    instrumentor.instrument(tracer_provider=provider)
    try:
        VoyageInstrumentor().instrument(tracer_provider=provider)
        client(fake).embed(TEXTS, model=EMBED_MODEL)
    finally:
        instrumentor.uninstrument()
    assert len(exporter.get_finished_spans()) == 1
    assert not hasattr(voyageai.Client.__dict__["embed"], "__wrapped__")


def test_a_client_built_before_instrument_is_traced(fake):
    # Methods are patched on the class, so construction order does not matter;
    # only calls made while instrumented are traced.
    early = client(fake)
    early.embed(TEXTS, model=EMBED_MODEL)
    with instrumented() as traced:
        early.embed(TEXTS, model=EMBED_MODEL)
    early.embed(TEXTS, model=EMBED_MODEL)
    assert len(traced.spans()) == 1
