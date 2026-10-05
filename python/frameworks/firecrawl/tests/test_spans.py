"""Span contract for the real firecrawl-py v2 clients against a loopback fake.

Every test drives the installed SDK (sync ``requests`` and async ``httpx``) at
``_firecrawl_fake.FakeFirecrawl`` and reads the spans from an in-memory
exporter. No call leaves 127.0.0.1 and every key is a placeholder.
"""

from __future__ import annotations

import asyncio
from typing import Any, Dict, Iterator, List, Tuple

import pytest
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import ReadableSpan, TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

pytest.importorskip("firecrawl", reason="firecrawl-py must be installed to test its instrumentor")

from _firecrawl_fake import JOB_ID, FakeFirecrawl  # noqa: E402
from traceai_firecrawl import FirecrawlInstrumentor  # noqa: E402

API_KEY = "fc-placeholder-key-must-not-be-exported"

Tracing = Tuple[TracerProvider, InMemorySpanExporter, FirecrawlInstrumentor]


@pytest.fixture()
def fake() -> Iterator[FakeFirecrawl]:
    server = FakeFirecrawl()
    yield server
    server.close()


@pytest.fixture()
def tracing() -> Iterator[Tracing]:
    exporter = InMemorySpanExporter()
    provider = TracerProvider(resource=Resource.create({"service.name": "test-firecrawl"}))
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    instrumentor = FirecrawlInstrumentor()
    instrumentor.instrument(tracer_provider=provider)
    try:
        yield provider, exporter, instrumentor
    finally:
        instrumentor.uninstrument()


def sync_client(fake: FakeFirecrawl) -> Any:
    from firecrawl import Firecrawl

    return Firecrawl(api_key=API_KEY, api_url=fake.origin, max_retries=1)


def async_client(fake: FakeFirecrawl) -> Any:
    # Build inside the running loop: the client owns an httpx.AsyncClient.
    from firecrawl import AsyncFirecrawl

    return AsyncFirecrawl(api_key=API_KEY, api_url=fake.origin, max_retries=1)


def spans(exporter: InMemorySpanExporter) -> List[ReadableSpan]:
    return list(exporter.get_finished_spans())


def attrs(span: ReadableSpan) -> Dict[str, Any]:
    return dict(span.attributes or {})


# R1: one span per user call. AsyncFirecrawlClient.crawl awaits self.start_crawl
# (firecrawl/v2/client_async.py), which is wrapped too; it must not add a span.


def test_async_crawl_emits_exactly_one_crawl_span(fake: FakeFirecrawl, tracing: Tracing) -> None:
    _, exporter, _ = tracing

    async def journey() -> Any:
        client = async_client(fake)
        job = await client.crawl(url="https://example.com", limit=3, poll_interval=0)
        await client.scrape("https://example.com/next")
        return job

    job = asyncio.run(journey())

    assert job.status == "completed" and len(job.data) == 3
    assert fake.calls_to("POST", "/v2/crawl") == 1
    names = [span.name for span in spans(exporter)]
    # The nested start_crawl adds nothing, and the guard is released afterwards.
    assert names == ["firecrawl.crawl", "firecrawl.scrape"]
    crawl = attrs(spans(exporter)[0])
    assert crawl["firecrawl.job_id"] == JOB_ID
    assert crawl["firecrawl.page_count"] == 3
    assert crawl["firecrawl.limit"] == 3


def test_sync_crawl_emits_exactly_one_crawl_span(fake: FakeFirecrawl, tracing: Tracing) -> None:
    _, exporter, _ = tracing

    client = sync_client(fake)
    job = client.crawl("https://example.com", limit=3, poll_interval=0)
    client.scrape("https://example.com/next")

    assert job.status == "completed"
    assert [span.name for span in spans(exporter)] == ["firecrawl.crawl", "firecrawl.scrape"]
    assert attrs(spans(exporter)[0])["firecrawl.job_id"] == JOB_ID
