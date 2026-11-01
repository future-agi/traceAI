"""Real v2 SDK operations against a loopback API; no vendor credentials needed."""

from __future__ import annotations

import asyncio
from contextlib import contextmanager

import pytest
from firecrawl import AsyncFirecrawl, Firecrawl
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from opentelemetry.trace import StatusCode

from _firecrawl_fake import FakeFirecrawl, PAGE_BODY, crawl_status
from traceai_firecrawl import FirecrawlInstrumentor

API_KEY = "fc-synthetic-private-key"
PRIVATE = "PRIVATE-CONTENT-MUST-NOT-LEAVE"
URLS = [f"https://example.com/{PRIVATE}?key={API_KEY}"]


@pytest.fixture()
def api():
    server = FakeFirecrawl()
    for family, job in (("extract", "extract-1"), ("agent", "agent-1")):
        server.routes[("POST", f"/v2/{family}")] = (200, {
            "success": True, "id": job, "status": "processing",
        })
        server.routes[("GET", f"/v2/{family}/{job}")] = (200, {
            "success": True, "id": job, "status": "completed", "creditsUsed": 2,
            "data": {"secret": PRIVATE}, "error": PRIVATE,
        })
    server.routes.update({
        ("DELETE", "/v2/agent/agent-1"): (200, {"success": True}),
        ("POST", "/v2/batch/scrape"): (200, {
            "success": True, "id": "batch-1", "url": URLS[0],
        }),
        ("GET", "/v2/batch/scrape/batch-1"): (200, crawl_status("completed", pages=2)),
        ("GET", "/v2/batch/scrape/batch-1/errors"): (200, {
            "success": True, "errors": [], "robotsBlocked": URLS,
        }),
        ("DELETE", "/v2/batch/scrape/batch-1"): (200, {
            "success": True, "status": "cancelled",
        }),
        ("POST", "/v2/browser"): (200, {
            "success": True, "id": "browser-1", "cdpUrl": URLS[0],
            "liveViewUrl": URLS[0], "interactiveLiveViewUrl": URLS[0],
        }),
        ("POST", "/v2/browser/browser-1/execute"): (200, {
            "success": True, "stdout": PRIVATE, "output": PRIVATE, "exitCode": 0,
        }),
        ("DELETE", "/v2/browser/browser-1"): (200, {
            "success": True, "status": "destroyed", "creditsBilled": 3,
        }),
        ("GET", "/v2/browser"): (200, {"success": True, "sessions": []}),
    })
    try:
        yield server
    finally:
        server.close()


@contextmanager
def trace_calls(config=None):
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    instrumentor = FirecrawlInstrumentor()
    instrumentor.instrument(tracer_provider=provider, config=config)
    try:
        yield exporter, provider, instrumentor
    finally:
        if instrumentor.is_instrumented_by_opentelemetry:
            instrumentor.uninstrument()
        provider.shutdown()


def invoke(api, is_async, calls):
    """Use the public facade, constructed after instrumentation."""
    if not is_async:
        client = Firecrawl(api_key=API_KEY, api_url=api.origin, max_retries=1)
        return [getattr(client, name)(*args, **kwargs) for name, args, kwargs in calls]

    async def run():
        client = AsyncFirecrawl(api_key=API_KEY, api_url=api.origin, max_retries=1)
        try:
            results = []
            for name, args, kwargs in calls:
                # wait_batch_scrape is public on the direct v2 client only.
                target = client._v2_client if name == "wait_batch_scrape" else client
                results.append(await getattr(target, name)(*args, **kwargs))
            return results
        finally:
            await client._v2_client.async_http_client.close()

    return asyncio.run(run())


OPERATIONS = [
    ("extract", [URLS], {"prompt": PRIVATE, "poll_interval": 0}),
    ("start_extract", [URLS], {"prompt": PRIVATE}),
    ("get_extract_status", ["extract-1"], {}),
    ("batch_scrape", [URLS], {"poll_interval": 0}),
    ("start_batch_scrape", [URLS], {}),
    ("get_batch_scrape_status", ["batch-1"], {}),
    ("get_batch_scrape_status_page", [], {}),
    ("get_batch_scrape_errors", ["batch-1"], {}),
    ("cancel_batch_scrape", ["batch-1"], {}),
    ("agent", [URLS], {"prompt": PRIVATE, "poll_interval": 0}),
    ("start_agent", [URLS], {"prompt": PRIVATE}),
    ("get_agent_status", ["agent-1"], {}),
    ("cancel_agent", ["agent-1"], {}),
    ("browser", [], {}),
    ("browser_execute", ["browser-1", PRIVATE], {}),
    ("delete_browser", ["browser-1"], {}),
    ("list_browsers", [], {}),
]


@pytest.mark.parametrize("is_async", [False, True], ids=["sync", "async"])
@pytest.mark.parametrize("operation", OPERATIONS, ids=[item[0] for item in OPERATIONS])
def test_real_sdk_operation_emits_one_private_span(api, is_async, operation):
    name, args, kwargs = operation
    if name == "get_batch_scrape_status_page":
        args = [api.origin + "/v2/batch/scrape/batch-1?cursor=" + PRIVATE]
    with trace_calls() as (exporter, provider, _):
        with provider.get_tracer(__name__).start_as_current_span("parent") as parent:
            result = invoke(api, is_async, [(name, args, kwargs)])[0]
        spans = [span for span in exporter.get_finished_spans() if span.name != "parent"]
    assert api.calls, "The installed SDK must reach the loopback transport"
    assert result is not None
    assert len(spans) == 1
    span = spans[0]
    assert span.name == "firecrawl." + name
    assert span.parent.span_id == parent.get_span_context().span_id
    assert span.attributes["gen_ai.span.kind"] == "TOOL"
    assert span.status.status_code == StatusCode.OK
    exported = span.to_json()
    for secret in (PRIVATE, API_KEY, PAGE_BODY, "https://example.com/"):
        assert secret not in exported


@pytest.mark.parametrize("is_async", [False, True], ids=["sync", "async"])
@pytest.mark.parametrize("keyword_ids", [False, True], ids=["positional", "keyword"])
@pytest.mark.parametrize("family", ["extract", "batch_scrape", "agent", "browser"])
def test_lifecycle_ids_and_cancellation(api, is_async, keyword_ids, family):
    if family == "browser":
        calls = [("browser", [], {}), ("browser_execute", ["browser-1"], {"code": PRIVATE}),
                 ("delete_browser", ["browser-1"], {})]
        expected_id, attribute, arg_name = "browser-1", "firecrawl.session_id", "session_id"
    else:
        expected_id = {"extract": "extract-1", "batch_scrape": "batch-1", "agent": "agent-1"}[family]
        calls = [("start_" + family, [URLS], {} if family == "batch_scrape" else {"prompt": PRIVATE}),
                 ("get_" + family + "_status", [expected_id], {})]
        if family != "extract":
            calls.append(("cancel_" + family, [expected_id], {}))
        attribute, arg_name = "firecrawl.job_id", "job_id"
    if keyword_ids:
        calls = [calls[0]] + [(name, [], {arg_name: args[0], **kwargs}) for name, args, kwargs in calls[1:]]
    with trace_calls() as (exporter, _, _):
        invoke(api, is_async, calls)
        spans = exporter.get_finished_spans()
    assert [span.attributes.get(attribute) for span in spans] == [expected_id] * len(calls)
    if family in ("batch_scrape", "agent"):
        assert spans[-1].attributes["firecrawl.cancelled"] is True
    if family == "browser":
        assert spans[-1].attributes["firecrawl.credits_used"] == 3
    else:
        # The async extract SDK drops the API's camel-case creditsUsed field.
        # Do not invent a zero or read raw transport payloads to recover it.
        if family == "extract" and is_async:
            assert "firecrawl.credits_used" not in spans[1].attributes
        else:
            assert spans[1].attributes["firecrawl.credits_used"] == 2
        assert spans[1].attributes["firecrawl.status"] == "completed"


@pytest.mark.parametrize("is_async", [False, True])
@pytest.mark.parametrize("family", ["extract", "batch_scrape", "agent"])
def test_failed_job_outcome_but_successful_status_poll(api, is_async, family):
    route, job = {"extract": ("extract", "extract-1"), "agent": ("agent", "agent-1"),
                  "batch_scrape": ("batch/scrape", "batch-1")}[family]
    status, payload = api.routes[("GET", f"/v2/{route}/{job}")]
    api.routes[("GET", f"/v2/{route}/{job}")] = (status, {**payload, "status": "failed"})
    kwargs = {"poll_interval": 0}
    if family != "batch_scrape":
        kwargs["prompt"] = PRIVATE
    with trace_calls() as (exporter, _, _):
        results = invoke(api, is_async, [(family, [URLS], kwargs), (f"get_{family}_status", [job], {})])
        spans = exporter.get_finished_spans()
    assert all(result.status == "failed" for result in results)
    assert len(spans) == 2
    assert spans[0].status.status_code == StatusCode.ERROR
    assert spans[0].status.description == "failed"
    assert spans[1].status.status_code == StatusCode.OK
    assert spans[1].attributes["firecrawl.status"] == "failed"


@pytest.mark.parametrize("is_async", [False, True])
def test_browser_failure_is_safe_and_preserves_response(api, is_async):
    api.routes[("POST", "/v2/browser/browser-1/execute")] = (200, {
        "success": False, "error": PRIVATE, "stderr": PRIVATE,
    })
    with trace_calls() as (exporter, _, _):
        result = invoke(api, is_async, [("browser_execute", ["browser-1", PRIVATE], {})])[0]
        (span,) = exporter.get_finished_spans()
    assert result.error == PRIVATE
    assert span.status.status_code == StatusCode.ERROR
    assert span.status.description == "failed"
    assert PRIVATE not in span.to_json()


def test_async_wait_batch_scrape_is_one_span(api):
    with trace_calls() as (exporter, _, _):
        invoke(api, True, [("wait_batch_scrape", ["batch-1"], {"poll_interval": 0})])
        (span,) = exporter.get_finished_spans()
    assert span.name == "firecrawl.wait_batch_scrape"
    assert span.attributes["firecrawl.job_id"] == "batch-1"
    assert span.attributes["firecrawl.page_count"] == 2


@pytest.mark.parametrize("is_async", [False, True])
@pytest.mark.parametrize("method", ["start_agent", "start_batch_scrape", "browser_execute"])
def test_api_error_keeps_safe_metadata(api, is_async, method):
    from firecrawl.v2.utils.error_handler import UnauthorizedError

    path, args, kwargs = {
        "start_agent": ("/v2/agent", [URLS], {"prompt": PRIVATE}),
        "start_batch_scrape": ("/v2/batch/scrape", [URLS], {}),
        "browser_execute": ("/v2/browser/browser-1/execute", ["browser-1", PRIVATE], {}),
    }[method]
    api.routes[("POST", path)] = (401, {"success": False, "error": PRIVATE + API_KEY})
    with trace_calls() as (exporter, _, _):
        if is_async and method == "browser_execute":
            # The SDK returns a failure model rather than raising for this API.
            result = invoke(api, is_async, [(method, args, kwargs)])[0]
            assert result.success is False and result.error == PRIVATE + API_KEY
        else:
            with pytest.raises(UnauthorizedError):
                invoke(api, is_async, [(method, args, kwargs)])
        (span,) = exporter.get_finished_spans()
    assert span.status.status_code == StatusCode.ERROR
    if is_async and method == "browser_execute":
        assert span.status.description == "failed"
        assert "firecrawl.error.status_code" not in span.attributes
        assert not span.events
    else:
        assert span.status.description == "UnauthorizedError"
        assert span.attributes["firecrawl.error.status_code"] == 401
        assert len(span.events) == 1
    assert PRIVATE not in span.to_json() and API_KEY not in span.to_json()


@pytest.mark.parametrize("is_async", [False, True])
@pytest.mark.parametrize("method,route,job", [
    ("extract", "extract", "extract-1"), ("batch_scrape", "batch/scrape", "batch-1"),
])
def test_cancelled_job_is_not_a_success(api, is_async, method, route, job):
    key = ("GET", f"/v2/{route}/{job}")
    status, payload = api.routes[key]
    api.routes[key] = status, {**payload, "status": "cancelled"}
    kwargs = {"poll_interval": 0}
    if method == "extract":
        kwargs["prompt"] = PRIVATE
    with trace_calls() as (exporter, _, _):
        invoke(api, is_async, [(method, [URLS], kwargs)])
        (span,) = exporter.get_finished_spans()
    assert span.status.description == "cancelled"
    assert span.status.status_code == StatusCode.ERROR
    assert span.attributes["firecrawl.cancelled"] is True


@pytest.mark.parametrize("is_async", [False, True])
def test_browser_nonzero_exit_is_not_a_success(api, is_async):
    api.routes[("POST", "/v2/browser/browser-1/execute")] = (200, {
        "success": True, "exitCode": 7, "stderr": PRIVATE,
    })
    with trace_calls() as (exporter, _, _):
        invoke(api, is_async, [("browser_execute", ["browser-1", PRIVATE], {})])
        (span,) = exporter.get_finished_spans()
    assert span.status.status_code == StatusCode.ERROR
    assert span.status.description == "execution_failed"
    assert PRIVATE not in span.to_json()


@pytest.mark.parametrize("is_async", [False, True])
def test_suppression_and_facade_uninstrument(api, is_async):
    from fi_instrumentation import suppress_tracing

    with trace_calls() as (exporter, _, instrumentor):
        with suppress_tracing():
            invoke(api, is_async, OPERATIONS[:6])
        assert not exporter.get_finished_spans()
        # A facade holds bound wrapper references even after class restoration.
        async def run():
            client_cls = AsyncFirecrawl if is_async else Firecrawl
            client = client_cls(api_key=API_KEY, api_url=api.origin, max_retries=1)
            instrumentor.uninstrument()
            try:
                result = client.start_agent(URLS, prompt=PRIVATE)
                if is_async:
                    result = await result
                assert result.id == "agent-1"
            finally:
                if is_async:
                    await client._v2_client.async_http_client.close()
        asyncio.run(run())
        assert not exporter.get_finished_spans()


def test_async_cancel_and_concurrent_calls_do_not_leak_active_guard(api, monkeypatch):
    from firecrawl.v2.utils.http_client_async import AsyncHttpClient

    original_post = AsyncHttpClient.post

    async def journey():
        entered = asyncio.Event()
        client = AsyncFirecrawl(api_key=API_KEY, api_url=api.origin, max_retries=1)

        async def post(transport, endpoint, *args, **kwargs):
            if endpoint == "/v2/browser":
                entered.set()
                await asyncio.Event().wait()
            return await original_post(transport, endpoint, *args, **kwargs)

        monkeypatch.setattr(AsyncHttpClient, "post", post)
        try:
            task = asyncio.create_task(client.browser())
            await asyncio.wait_for(entered.wait(), timeout=2)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
            await asyncio.gather(
                client.agent(URLS, prompt=PRIVATE, poll_interval=0),
                client.batch_scrape(URLS, poll_interval=0),
            )
        finally:
            await client._v2_client.async_http_client.close()

    with trace_calls() as (exporter, _, _):
        asyncio.run(journey())
        spans = exporter.get_finished_spans()
    assert sorted(span.name for span in spans) == ["firecrawl.agent", "firecrawl.batch_scrape", "firecrawl.browser"]
    cancelled = next(span for span in spans if span.name == "firecrawl.browser")
    assert cancelled.attributes["firecrawl.cancelled"] is True
    assert cancelled.status.description == "cancelled"


@pytest.mark.parametrize("is_async", [False, True])
@pytest.mark.parametrize("method", ["start_batch_scrape", "batch_scrape", "start_extract", "extract"])
def test_formats_only_not_prompt_or_schema(api, is_async, method):
    from firecrawl.v2.types import ScrapeOptions
    from fi_instrumentation.instrumentation import TraceConfig

    formats = [{"type": "json", "prompt": PRIVATE, "schema": {"type": "object"}}]
    payloads = []
    if "batch" in method:
        api.routes[("POST", "/v2/batch/scrape")] = (200, lambda body: (
            payloads.append(body) or {"success": True, "id": "batch-1", "url": URLS[0]}
        ))
        # Sync exposes convenience formats; async forwards only options to _prepare.
        kwargs = {"options": ScrapeOptions(formats=formats), "formats": ["html"]} if is_async else {"formats": formats}
    else:
        kwargs = {"prompt": PRIVATE, "scrape_options": ScrapeOptions(formats=formats)}
    with trace_calls(TraceConfig(hide_inputs=True, hide_outputs=True)) as (exporter, _, _):
        invoke(api, is_async, [(method, [URLS], kwargs)])
        (span,) = exporter.get_finished_spans()
    assert span.attributes["firecrawl.formats"] == ("json",)
    if payloads:
        assert [item["type"] if isinstance(item, dict) else item for item in payloads[0]["formats"]] == ["json"]
    assert "input.value" not in span.attributes and "output.value" not in span.attributes
    assert PRIVATE not in span.to_json()


@pytest.mark.parametrize("is_async", [False, True], ids=["sync", "async"])
def test_killed_browser_execution_is_failure_even_without_exit_code(api, is_async):
    api.routes[("POST", "/v2/browser/browser-1/execute")] = (200, {
        "success": True, "killed": True, "exitCode": None, "output": PRIVATE,
    })
    with trace_calls() as (exporter, _, _):
        result = invoke(api, is_async, [("browser_execute", ["browser-1", PRIVATE], {})])[0]
        (span,) = exporter.get_finished_spans()
    assert result.success is True and result.killed is True and result.exit_code is None
    assert span.status.status_code == StatusCode.ERROR
    assert span.status.description == "execution_failed"
    assert PRIVATE not in span.to_json()


@pytest.mark.parametrize("is_async", [False, True], ids=["sync", "async"])
@pytest.mark.parametrize("method,job", [("extract", "extract-1"), ("agent", "agent-1")])
def test_blocking_job_exhausted_wait_is_not_success(api, is_async, method, job):
    api.routes[("GET", f"/v2/{method}/{job}")] = (200, {
        "success": True, "id": job, "status": "processing", "data": {"secret": PRIVATE},
    })
    with trace_calls() as (exporter, _, _):
        result, poll = invoke(api, is_async, [
            (method, [URLS], {"prompt": PRIVATE, "timeout": 0}),
            (f"get_{method}_status", [job], {}),
        ])
        span, poll_span = exporter.get_finished_spans()
    assert result.status == poll.status == "processing"
    assert span.attributes["firecrawl.status"] == "processing"
    assert span.status.status_code == StatusCode.ERROR
    assert span.status.description == "processing"
    assert poll_span.status.status_code == StatusCode.OK
    assert PRIVATE not in span.to_json() and PRIVATE not in poll_span.to_json()


@pytest.mark.parametrize("method", ["start_batch_scrape", "batch_scrape"])
def test_async_batch_ignores_top_level_formats_without_options(api, method):
    payloads = []
    api.routes[("POST", "/v2/batch/scrape")] = (200, lambda body: (
        payloads.append(body) or {"success": True, "id": "batch-1", "url": URLS[0]}
    ))
    with trace_calls() as (exporter, _, _):
        invoke(api, True, [(method, [URLS], {"formats": ["json"], "poll_interval": 0})])
        (span,) = exporter.get_finished_spans()
    assert "formats" not in payloads[0]
    assert "firecrawl.formats" not in span.attributes
