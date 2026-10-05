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
from opentelemetry.trace import StatusCode

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


# R2 / AC-03: start, status and cancel share firecrawl.job_id, including when the
# id is passed positionally (cancel_crawl returns a bool, so the id must come
# from the call) and when the status call fails.

TRIO = ["firecrawl.start_crawl", "firecrawl.get_crawl_status", "firecrawl.cancel_crawl"]


def test_sync_trio_with_positional_ids_shares_job_id(fake: FakeFirecrawl, tracing: Tracing) -> None:
    _, exporter, _ = tracing

    client = sync_client(fake)
    started = client.start_crawl("https://example.com", limit=3)
    client.get_crawl_status(started.id)
    assert client.cancel_crawl(started.id) is True

    assert [span.name for span in spans(exporter)] == TRIO
    assert [attrs(span).get("firecrawl.job_id") for span in spans(exporter)] == [JOB_ID] * 3


def test_async_trio_with_positional_ids_shares_job_id(fake: FakeFirecrawl, tracing: Tracing) -> None:
    _, exporter, _ = tracing

    async def journey() -> None:
        client = async_client(fake)
        started = await client.start_crawl("https://example.com", limit=3)
        await client.get_crawl_status(started.id)
        assert await client.cancel_crawl(started.id) is True

    asyncio.run(journey())

    assert [span.name for span in spans(exporter)] == TRIO
    assert [attrs(span).get("firecrawl.job_id") for span in spans(exporter)] == [JOB_ID] * 3


def test_failed_status_and_cancel_calls_keep_positional_job_id(
    fake: FakeFirecrawl, tracing: Tracing
) -> None:
    _, exporter, _ = tracing
    client = sync_client(fake)

    with pytest.raises(Exception):
        client.get_crawl_status("missing-job")
    with pytest.raises(Exception):
        client.cancel_crawl("missing-job")

    async def journey() -> None:
        aclient = async_client(fake)
        with pytest.raises(Exception):
            await aclient.get_crawl_status("missing-job")
        with pytest.raises(Exception):
            await aclient.cancel_crawl("missing-job")

    asyncio.run(journey())

    finished = spans(exporter)
    assert [span.name for span in finished] == [
        "firecrawl.get_crawl_status",
        "firecrawl.cancel_crawl",
    ] * 2
    for span in finished:
        assert span.status.status_code is StatusCode.ERROR
        assert attrs(span)["firecrawl.job_id"] == "missing-job"


# R3 / J3: the SDK returns a failed or cancelled job without raising, so the span
# takes its status from the job. failed -> ERROR; cancelled -> ERROR "cancelled"
# plus firecrawl.cancelled=true; completed -> OK.

EXPECTED_JOB_STATUS = {
    "completed": (StatusCode.OK, None),
    "failed": (StatusCode.ERROR, "failed"),
    "cancelled": (StatusCode.ERROR, "cancelled"),
}


def _crawl_and_status(fake: FakeFirecrawl, use_async: bool) -> None:
    if not use_async:
        client = sync_client(fake)
        client.crawl("https://example.com", limit=3, poll_interval=0)
        client.get_crawl_status(JOB_ID)
        return

    async def journey() -> None:
        client = async_client(fake)
        await client.crawl(url="https://example.com", limit=3, poll_interval=0)
        await client.get_crawl_status(JOB_ID)

    asyncio.run(journey())


@pytest.mark.parametrize("use_async", [False, True], ids=["sync", "async"])
@pytest.mark.parametrize("job_status", sorted(EXPECTED_JOB_STATUS))
def test_job_status_sets_attribute_and_span_status(
    fake: FakeFirecrawl, tracing: Tracing, job_status: str, use_async: bool
) -> None:
    from _firecrawl_fake import crawl_status

    _, exporter, _ = tracing
    fake.routes[("GET", "/v2/crawl/" + JOB_ID)] = (200, crawl_status(job_status))

    _crawl_and_status(fake, use_async)

    finished = spans(exporter)
    assert [span.name for span in finished] == ["firecrawl.crawl", "firecrawl.get_crawl_status"]
    code, description = EXPECTED_JOB_STATUS[job_status]
    for span in finished:
        assert attrs(span)["firecrawl.status"] == job_status
        assert span.status.status_code is code
        if description is not None:
            assert span.status.description == description
        if job_status == "cancelled":
            assert attrs(span)["firecrawl.cancelled"] is True
        else:
            assert "firecrawl.cancelled" not in attrs(span)
