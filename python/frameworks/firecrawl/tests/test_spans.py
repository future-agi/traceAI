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


# R3 / J3: the SDK returns a failed or cancelled job without raising, so the
# blocking crawl() span takes its status from the job. failed -> ERROR;
# cancelled -> ERROR "cancelled" plus firecrawl.cancelled=true; completed -> OK.
# get_crawl_status only records the job's state.

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
    crawl_span, status_span = finished
    code, description = EXPECTED_JOB_STATUS[job_status]
    # The blocking crawl() call's outcome is the job's outcome.
    assert attrs(crawl_span)["firecrawl.status"] == job_status
    assert crawl_span.status.status_code is code
    if description is not None:
        assert crawl_span.status.description == description
    if job_status == "cancelled":
        assert attrs(crawl_span)["firecrawl.cancelled"] is True
    else:
        assert "firecrawl.cancelled" not in attrs(crawl_span)
    # A status poll that got an answer succeeded, whatever the job's state: it
    # records the state, and only the poll's own errors make it ERROR.
    assert attrs(status_span)["firecrawl.status"] == job_status
    assert status_span.status.status_code is StatusCode.OK
    assert "firecrawl.cancelled" not in attrs(status_span)


# R5: instrumentation errors never reach the caller, and the span always ends.


def test_unparseable_url_does_not_break_the_call(fake: FakeFirecrawl, tracing: Tracing) -> None:
    # urlsplit raises ValueError("Invalid IPv6 URL") for this string; the SDK
    # itself sends it to the API unchanged.
    _, exporter, _ = tracing
    bad_url = "http://[::1/page"

    document = sync_client(fake).scrape(bad_url)

    async def journey() -> Any:
        return await async_client(fake).scrape(bad_url)

    async_document = asyncio.run(journey())

    assert document.markdown and async_document.markdown
    finished = spans(exporter)
    assert [span.name for span in finished] == ["firecrawl.scrape"] * 2
    assert all(attrs(span)["fi.span.kind"] == "TOOL" for span in finished)


class _ExplodingResult:
    """A result whose attributes raise something other than AttributeError."""

    @property
    def data(self) -> Any:
        raise RuntimeError("result attribute exploded")

    web = data
    credits_used = data


def test_result_attribute_errors_do_not_reach_the_caller(
    monkeypatch: pytest.MonkeyPatch, fake: FakeFirecrawl
) -> None:
    from firecrawl.v2.client import FirecrawlClient
    from firecrawl.v2.client_async import AsyncFirecrawlClient

    result = _ExplodingResult()

    def search(_self: Any, _query: str, **_kwargs: Any) -> Any:
        return result

    async def asearch(_self: Any, _query: str, **_kwargs: Any) -> Any:
        return result

    monkeypatch.setattr(FirecrawlClient, "search", search)
    monkeypatch.setattr(AsyncFirecrawlClient, "search", asearch)
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    instrumentor = FirecrawlInstrumentor()
    instrumentor.instrument(tracer_provider=provider)
    try:
        assert sync_client(fake).search("query") is result

        async def journey() -> Any:
            return await async_client(fake).search("query")

        assert asyncio.run(journey()) is result
    finally:
        instrumentor.uninstrument()

    assert [span.name for span in spans(exporter)] == ["firecrawl.search"] * 2


class _UnprintableError(Exception):
    def __str__(self) -> str:
        raise RuntimeError("str() exploded")


def test_error_recording_failure_still_reraises_the_vendor_error(
    monkeypatch: pytest.MonkeyPatch, fake: FakeFirecrawl
) -> None:
    from firecrawl.v2.client import FirecrawlClient

    def scrape(_self: Any, _url: str, **_kwargs: Any) -> Any:
        raise _UnprintableError()

    monkeypatch.setattr(FirecrawlClient, "scrape", scrape)
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    instrumentor = FirecrawlInstrumentor()
    instrumentor.instrument(tracer_provider=provider)
    try:
        with pytest.raises(_UnprintableError):
            sync_client(fake).scrape("https://example.com")
    finally:
        instrumentor.uninstrument()

    finished = spans(exporter)
    assert [span.name for span in finished] == ["firecrawl.scrape"]
    assert finished[0].status.status_code is StatusCode.ERROR


# R6: the vendor call runs with the TOOL span current, so HTTP client spans
# (and anything else the SDK traces) nest under it.


def test_http_spans_nest_under_the_tool_span(
    monkeypatch: pytest.MonkeyPatch, fake: FakeFirecrawl, tracing: Tracing
) -> None:
    from firecrawl.v2.utils.http_client import HttpClient
    from firecrawl.v2.utils.http_client_async import AsyncHttpClient
    from opentelemetry import trace

    provider, exporter, _ = tracing
    http_tracer = provider.get_tracer("fake-http-instrumentation")
    sync_post, async_post = HttpClient.post, AsyncHttpClient.post

    def traced_post(self: Any, *args: Any, **kwargs: Any) -> Any:
        with http_tracer.start_as_current_span("HTTP POST"):
            return sync_post(self, *args, **kwargs)

    async def traced_async_post(self: Any, *args: Any, **kwargs: Any) -> Any:
        with http_tracer.start_as_current_span("HTTP POST"):
            return await async_post(self, *args, **kwargs)

    monkeypatch.setattr(HttpClient, "post", traced_post)
    monkeypatch.setattr(AsyncHttpClient, "post", traced_async_post)

    sync_client(fake).scrape("https://example.com")

    async def journey() -> None:
        await async_client(fake).scrape("https://example.com")

    asyncio.run(journey())
    # The TOOL span is not left current after the call returns.
    assert not trace.get_current_span().get_span_context().is_valid

    finished = spans(exporter)
    assert [span.name for span in finished] == ["HTTP POST", "firecrawl.scrape"] * 2
    for http_span, tool_span in (finished[0:2], finished[2:4]):
        assert http_span.parent is not None
        assert http_span.parent.span_id == tool_span.context.span_id
        assert http_span.context.trace_id == tool_span.context.trace_id


def test_vendor_error_is_recorded_once_and_reraised(fake: FakeFirecrawl, tracing: Tracing) -> None:
    from firecrawl.v2.utils.error_handler import FirecrawlError

    _, exporter, _ = tracing
    fake.routes[("POST", "/v2/scrape")] = (500, {"success": False, "error": "internal error"})

    with pytest.raises(FirecrawlError):
        sync_client(fake).scrape("https://example.com")

    (span,) = spans(exporter)
    assert span.status.status_code is StatusCode.ERROR
    assert [event.name for event in span.events] == ["exception"]


# R7 / AC-07: cancellation sets firecrawl.cancelled=true. A cancelled asyncio task
# running a blocking crawl ends the span ERROR "cancelled"; cancel_crawl records
# whether the API cancelled the job.


def test_cancelling_an_async_crawl_task_marks_the_span_cancelled(
    fake: FakeFirecrawl, tracing: Tracing
) -> None:
    from _firecrawl_fake import crawl_status

    _, exporter, _ = tracing
    fake.routes[("GET", "/v2/crawl/" + JOB_ID)] = (200, crawl_status("scraping"))

    async def journey() -> None:
        client = async_client(fake)
        task = asyncio.ensure_future(
            client.crawl(url="https://example.com", limit=3, poll_interval=0.01)
        )
        for _ in range(500):
            if fake.calls_to("GET", "/v2/crawl/" + JOB_ID):
                break
            await asyncio.sleep(0.01)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(journey())

    (span,) = spans(exporter)
    assert span.name == "firecrawl.crawl"
    assert span.status.status_code is StatusCode.ERROR
    assert span.status.description == "cancelled"
    assert attrs(span)["firecrawl.cancelled"] is True


@pytest.mark.parametrize("api_status,cancelled", [("cancelled", True), ("scraping", False)])
def test_cancel_crawl_records_whether_the_job_was_cancelled(
    fake: FakeFirecrawl, tracing: Tracing, api_status: str, cancelled: bool
) -> None:
    _, exporter, _ = tracing
    fake.routes[("DELETE", "/v2/crawl/" + JOB_ID)] = (200, {"success": True, "status": api_status})

    assert sync_client(fake).cancel_crawl(JOB_ID) is cancelled

    async def journey() -> Any:
        return await async_client(fake).cancel_crawl(JOB_ID)

    assert asyncio.run(journey()) is cancelled

    finished = spans(exporter)
    assert [span.name for span in finished] == ["firecrawl.cancel_crawl"] * 2
    for span in finished:
        assert attrs(span)["firecrawl.cancelled"] is cancelled
        assert span.status.status_code is StatusCode.OK


# R7 / J1: requested formats are recorded as a list of format names. A JSON
# format's prompt or schema is content and stays off the span.


def test_requested_formats_are_recorded_by_name(fake: FakeFirecrawl, tracing: Tracing) -> None:
    from firecrawl.v2.types import JsonFormat, ScrapeOptions

    _, exporter, _ = tracing
    prompt = "FORMAT-PROMPT-MUST-NOT-BE-EXPORTED"
    client = sync_client(fake)
    client.scrape("https://example.com", formats=["markdown", JsonFormat(prompt=prompt)])
    client.scrape("https://example.com")
    client.start_crawl("https://example.com", scrape_options=ScrapeOptions(formats=["html"]))

    async def journey() -> None:
        await async_client(fake).scrape(
            "https://example.com", formats=["markdown", {"type": "json", "prompt": prompt}]
        )

    asyncio.run(journey())

    finished = spans(exporter)
    assert [attrs(span).get("firecrawl.formats") for span in finished] == [
        ("markdown", "json"),
        None,
        ("html",),
        ("markdown", "json"),
    ]
    assert all(prompt not in str(attrs(span)) for span in finished)


# R7 / J5: a vendor error records its HTTP status and machine-readable code.


@pytest.mark.parametrize("use_async", [False, True], ids=["sync", "async"])
def test_vendor_error_records_status_code_and_code(
    fake: FakeFirecrawl, tracing: Tracing, use_async: bool
) -> None:
    from firecrawl.v2.utils.error_handler import RateLimitError

    _, exporter, _ = tracing
    fake.routes[("POST", "/v2/scrape")] = (
        429,
        {"success": False, "error": "rate limited", "code": "RATE_LIMIT_EXCEEDED"},
    )

    with pytest.raises(RateLimitError):
        if use_async:

            async def journey() -> None:
                await async_client(fake).scrape("https://example.com")

            asyncio.run(journey())
        else:
            sync_client(fake).scrape("https://example.com")

    (span,) = spans(exporter)
    assert span.status.status_code is StatusCode.ERROR
    assert attrs(span)["firecrawl.error.status_code"] == 429
    assert attrs(span)["firecrawl.error.code"] == "RATE_LIMIT_EXCEEDED"
    assert "firecrawl.cancelled" not in attrs(span)


# R8 / AC-06: the API key is redacted from every recorded string. firecrawl-py
# keeps it on the client's HTTP clients, not on the client itself.


def test_api_key_is_redacted_from_query_and_job_id(
    monkeypatch: pytest.MonkeyPatch, fake: FakeFirecrawl, tracing: Tracing
) -> None:
    _, exporter, _ = tracing
    leaky_id = "job-" + API_KEY
    fake.routes[("POST", "/v2/crawl")] = (200, {"success": True, "id": leaky_id, "url": "unused"})

    client = sync_client(fake)
    client.search("find " + API_KEY)
    client.start_crawl("https://example.com")

    async def journey() -> None:
        aclient = async_client(fake)
        await aclient.search("find " + API_KEY)
        await aclient.start_crawl("https://example.com")

    asyncio.run(journey())

    # A key taken from FIRECRAWL_API_KEY is redacted too.
    monkeypatch.setenv("FIRECRAWL_API_KEY", API_KEY)
    from firecrawl import Firecrawl

    Firecrawl(api_url=fake.origin, max_retries=1).search("find " + API_KEY)

    finished = spans(exporter)
    assert [span.name for span in finished] == [
        "firecrawl.search",
        "firecrawl.start_crawl",
        "firecrawl.search",
        "firecrawl.start_crawl",
        "firecrawl.search",
    ]
    for span in finished:
        assert all(API_KEY not in str(value) for value in attrs(span).values()), span.name
    assert attrs(finished[0])["fi.retrieval.query"] == "find [redacted]"
    assert attrs(finished[1])["firecrawl.job_id"] == "job-[redacted]"


# Coverage the installed review listed as missing.

OPERATIONS = (
    "scrape",
    "search",
    "map",
    "crawl",
    "start_crawl",
    "get_crawl_status",
    "cancel_crawl",
)


def test_map_emits_one_tool_span_without_links(fake: FakeFirecrawl, tracing: Tracing) -> None:
    from _firecrawl_fake import RESULT_TITLE

    _, exporter, _ = tracing
    mapped = sync_client(fake).map("https://example.com/private/start", limit=2)

    async def journey() -> Any:
        return await async_client(fake).map("https://example.com/private/start", limit=2)

    async_mapped = asyncio.run(journey())

    assert len(mapped.links) == 2 and len(async_mapped.links) == 2
    finished = spans(exporter)
    assert [span.name for span in finished] == ["firecrawl.map"] * 2
    for span in finished:
        assert span.status.status_code is StatusCode.OK
        assert attrs(span) == {"fi.span.kind": "TOOL", "server.address": "example.com"}
        assert "/private" not in str(attrs(span)) and RESULT_TITLE not in str(attrs(span))


def _sync_journey(fake: FakeFirecrawl) -> None:
    client = sync_client(fake)
    client.scrape("https://example.com/a", formats=["markdown"])
    client.search("open telemetry", limit=2)
    client.map("https://example.com")
    client.crawl("https://example.com", limit=3, poll_interval=0)
    client.start_crawl("https://example.com", limit=3)
    client.get_crawl_status(JOB_ID)
    client.cancel_crawl(JOB_ID)


async def _async_journey(fake: FakeFirecrawl) -> None:
    client = async_client(fake)
    await client.scrape("https://example.com/a", formats=["markdown"])
    await client.search("open telemetry", limit=2)
    await client.map("https://example.com")
    await client.crawl(url="https://example.com", limit=3, poll_interval=0)
    await client.start_crawl("https://example.com", limit=3)
    await client.get_crawl_status(JOB_ID)
    await client.cancel_crawl(JOB_ID)


def test_async_twins_record_the_same_spans_as_sync(fake: FakeFirecrawl, tracing: Tracing) -> None:
    """AC-08 / J6: AsyncFirecrawl emits the same span names, attributes and status."""
    _, exporter, _ = tracing

    _sync_journey(fake)
    sync_spans = spans(exporter)
    exporter.clear()
    asyncio.run(_async_journey(fake))
    async_spans = spans(exporter)

    def summary(finished: List[ReadableSpan]) -> List[Tuple[str, Dict[str, Any], StatusCode]]:
        return [(span.name, attrs(span), span.status.status_code) for span in finished]

    assert [span.name for span in sync_spans] == ["firecrawl." + name for name in OPERATIONS]
    assert summary(async_spans) == summary(sync_spans)


def test_uninstrument_restores_methods_and_stops_new_spans(fake: FakeFirecrawl) -> None:
    from firecrawl.v2.client import FirecrawlClient
    from firecrawl.v2.client_async import AsyncFirecrawlClient

    classes = (FirecrawlClient, AsyncFirecrawlClient)
    originals = {(cls, name): cls.__dict__[name] for cls in classes for name in OPERATIONS}
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    instrumentor = FirecrawlInstrumentor()
    instrumentor.instrument(tracer_provider=provider)
    try:
        assert all(cls.__dict__[name] is not original for (cls, name), original in originals.items())
    finally:
        instrumentor.uninstrument()

    assert all(cls.__dict__[name] is original for (cls, name), original in originals.items())
    _sync_journey(fake)
    asyncio.run(_async_journey(fake))
    assert spans(exporter) == []


def test_facade_methods_bind_at_construction(fake: FakeFirecrawl) -> None:
    """R4, documented in the README: Firecrawl/AsyncFirecrawl copy the v2 client's
    bound methods in __init__. Build the client after instrument(); a client built
    before is not traced, and one built while instrumented keeps tracing after
    uninstrument()."""
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    instrumentor = FirecrawlInstrumentor()

    built_before = sync_client(fake)
    instrumentor.instrument(tracer_provider=provider)
    try:
        built_during = sync_client(fake)
        built_before.scrape("https://before.example.com")
        built_during.scrape("https://during.example.com")
    finally:
        instrumentor.uninstrument()
    built_during.scrape("https://during-after-uninstrument.example.com")
    sync_client(fake).scrape("https://after.example.com")

    assert [attrs(span)["server.address"] for span in spans(exporter)] == [
        "during.example.com",
        "during-after-uninstrument.example.com",
    ]
