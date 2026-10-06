"""Bounded review regressions through the real SDK and OTLP Receiver.

All network traffic goes to loopback fixtures; every credential is synthetic.
"""
from __future__ import annotations

import asyncio
from functools import wraps
from importlib import import_module
import json

import pytest

from _firecrawl_fake import FakeFirecrawl, PAGE_BODY
from harness import Receiver, _flatten_attributes
from traceai_firecrawl import FirecrawlInstrumentor

KEY = "fc-regression-placeholder-key"
PRIVATE_URL = "https://example.com/private-review-sentinel?secret=review"
HTML = "<html>PRIVATE-PAGE-REVIEW-SENTINEL</html>"


@pytest.fixture()
def fake():
    server = FakeFirecrawl()
    try:
        yield server
    finally:
        server.close()


@pytest.fixture()
def telemetry(monkeypatch):
    from fi_instrumentation import register
    from fi_instrumentation.fi_types import ProjectType

    with Receiver() as receiver:
        monkeypatch.setenv("FI_BASE_URL", receiver.origin)
        monkeypatch.setenv("FI_API_KEY", "placeholder-fi-api-key")
        monkeypatch.setenv("FI_SECRET_KEY", "placeholder-fi-secret-key")
        provider = register(project_name="firecrawl-review", project_type=ProjectType.OBSERVE,
                            verbose=False)
        instrumentor = FirecrawlInstrumentor()
        try:
            yield receiver, provider, instrumentor
        finally:
            if instrumentor.is_instrumented_by_opentelemetry:
                instrumentor.uninstrument()
            provider.shutdown()


def invoke(fake, use_async, method="scrape", target=PRIVATE_URL, env_key=False, **kwargs):
    from firecrawl import AsyncFirecrawl, Firecrawl

    options = dict(api_url=fake.origin, max_retries=1)
    if not env_key:
        options["api_key"] = KEY
    if use_async:
        async def journey():
            client = AsyncFirecrawl(**options)
            return await getattr(client, method)(target, **kwargs)
        return asyncio.run(journey())
    return getattr(Firecrawl(**options), method)(target, **kwargs)


def exported(telemetry):
    receiver, provider, _ = telemetry
    assert provider.force_flush(timeout_millis=10_000)
    spans = receiver.spans()
    return spans, json.dumps(spans) + json.dumps(receiver.requests())


def format_names(span):
    value = _flatten_attributes(span["attributes"]).get("firecrawl.formats")
    if value is None:
        return None
    return [item["stringValue"] for item in value["arrayValue"]["values"]]


def observe_vendor(monkeypatch, use_async, method):
    """Observe the real SDK's exact result/exception without replacing it."""
    module = import_module("firecrawl.v2.client_async" if use_async else "firecrawl.v2.client")
    cls = getattr(module, "AsyncFirecrawlClient" if use_async else "FirecrawlClient")
    original = getattr(cls, method)
    observed = []
    if use_async:
        @wraps(original)
        async def call(*args, **kwargs):
            try:
                result = await original(*args, **kwargs)
            except BaseException as error:
                observed.append(error)
                raise
            observed.append(result)
            return result
    else:
        @wraps(original)
        def call(*args, **kwargs):
            try:
                result = original(*args, **kwargs)
            except BaseException as error:
                observed.append(error)
                raise
            observed.append(result)
            return result
    monkeypatch.setattr(cls, method, call)
    return observed


@pytest.mark.parametrize("use_async", [False, True], ids=["sync", "async"])
@pytest.mark.parametrize("error_shape", ["error", "details", "nonjson", "validation", "unsafe-code"])
def test_f1_exception_wire_omits_untrusted_text(fake, telemetry, monkeypatch, use_async, error_shape):
    from pydantic import ValidationError
    from firecrawl.v2.utils.error_handler import FirecrawlError

    sentinel = " ".join((KEY, PRIVATE_URL, HTML))
    options = {}
    if error_shape == "validation":
        options["timeout"] = sentinel
        expected = ValidationError
    else:
        body = {"success": False, "error": "failed", "code": "RATE_LIMIT_EXCEEDED"}
        if error_shape == "nonjson":
            body = sentinel.encode()
        elif error_shape == "unsafe-code":
            body["code"] = sentinel
        else:
            body[error_shape] = sentinel
        fake.routes[("POST", "/v2/scrape")] = (400, body)
        expected = FirecrawlError
    observed = observe_vendor(monkeypatch, use_async, "scrape")
    telemetry[2].instrument(tracer_provider=telemetry[1])
    with pytest.raises(expected) as caught:
        invoke(fake, use_async, **options)
    assert caught.value is observed[0]
    # A control demonstrates that the vendor actually received the markers.
    if error_shape == "validation":
        assert caught.value.errors()[0]["input"] == sentinel
    elif error_shape == "unsafe-code":
        assert caught.value.code == sentinel
    else:
        assert sentinel in str(caught.value)
    spans, wire = exported(telemetry)
    assert len(spans) == 1
    span = spans[0]
    assert span["status"]["code"] == "STATUS_CODE_ERROR"
    assert len(span["events"]) == 1 and span["events"][0]["name"] == "exception"
    for secret in (KEY, KEY[:16], PRIVATE_URL, HTML, "REVIEW-SENTINEL"):
        assert secret not in wire
    event = _flatten_attributes(span["events"][0]["attributes"])
    assert "exception.stacktrace" not in event
    assert event["exception.type"] == type(caught.value).__name__
    assert span["status"]["message"] == type(caught.value).__name__
    attributes = _flatten_attributes(span["attributes"])
    if error_shape not in ("validation", "nonjson", "unsafe-code"):
        assert attributes["firecrawl.error.code"] == "RATE_LIMIT_EXCEEDED"
    else:
        assert "firecrawl.error.code" not in attributes


@pytest.mark.parametrize("use_async", [False, True], ids=["sync", "async"])
@pytest.mark.parametrize("env_key", [False, True], ids=["explicit-key", "env-key"])
@pytest.mark.parametrize("shape", ["dict", "boundary-dict", "string"])
@pytest.mark.parametrize("failed", [False, True], ids=["success", "failure"])
def test_f2_format_labels_are_canonical(fake, telemetry, monkeypatch, use_async, env_key, shape, failed):
    from firecrawl.v2.types import JsonFormat

    monkeypatch.setenv("FIRECRAWL_API_KEY", KEY)
    name = ("x" * 60 if shape == "boundary-dict" else "") + KEY
    invalid = name if shape == "string" else {"type": name, "prompt": HTML, "schema": {"title": KEY}}
    observed = observe_vendor(monkeypatch, use_async, "scrape")
    telemetry[2].instrument(tracer_provider=telemetry[1])
    options = dict(formats=["html", invalid, {"type": "person@example.test"},
                           JsonFormat(prompt=HTML, schema={"title": KEY}), "html"])
    if failed:
        fake.routes[("POST", "/v2/scrape")] = (400, {"success": False, "error": HTML})
    if failed or shape == "string":
        with pytest.raises(Exception) as caught:
            invoke(fake, use_async, env_key=env_key, **options)
        assert caught.value is observed[0]
    else:
        result = invoke(fake, use_async, env_key=env_key, **options)
        assert result is observed[0] and result.markdown == PAGE_BODY
    spans, wire = exported(telemetry)
    assert len(spans) == 1
    assert format_names(spans[0]) == ["html", "json"]
    for secret in (KEY, KEY[:4], "x" * 60, "person@example.test", HTML):
        assert secret not in wire


@pytest.mark.parametrize("use_async", [False, True], ids=["sync", "async"])
def test_f2_format_aliases_match_vendor_payload(fake, telemetry, use_async):
    from firecrawl.v2.types import Format, QuestionFormat, HighlightsFormat

    payloads = []
    def response(body):
        payloads.append(body)
        return {"success": True, "data": {"markdown": PAGE_BODY}}
    fake.routes[("POST", "/v2/scrape")] = (200, response)
    telemetry[2].instrument(tracer_provider=telemetry[1])
    invoke(fake, use_async, formats=["raw_html", "rawHtml", Format(type="audio"),
           {"type": "change_tracking", "modes": ["git-diff"]},
           QuestionFormat(question=HTML), HighlightsFormat(query=HTML)])
    spans, wire = exported(telemetry)
    assert format_names(spans[0]) == [
        "rawHtml", "audio", "changeTracking", "question", "highlights"]
    actual = [item if isinstance(item, str) else item["type"] for item in payloads[0]["formats"]]
    assert list(dict.fromkeys(actual)) == ["rawHtml", "audio", "changeTracking", "question", "highlights"]
    assert HTML not in wire


def test_f5_readme_usage_registers_observe(fake, telemetry):
    import ast
    from pathlib import Path

    readme = Path(__file__).resolve().parents[1] / "README.md"
    snippet = readme.read_text().split("```python\n", 1)[1].split("```", 1)[0]
    tree = ast.parse(snippet)
    redirected = 0
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "Firecrawl":
            # Execute the documented public path with just the network endpoint
            # redirected to loopback. Preserve its register/import arguments.
            node.keywords.append(ast.keyword(arg="api_url", value=ast.Constant(fake.origin)))
            redirected += 1
    assert redirected == 1
    namespace = {}
    try:
        exec(compile(ast.fix_missing_locations(tree), "README.md usage", "exec"), namespace)
        assert namespace["tracer_provider"].force_flush(timeout_millis=10_000)
    finally:
        if telemetry[2].is_instrumented_by_opentelemetry:
            telemetry[2].uninstrument()
        if "tracer_provider" in namespace:
            namespace["tracer_provider"].shutdown()
    assert fake.calls == [("POST", "/v2/scrape")]
    exports = telemetry[0].requests()
    assert exports and len(telemetry[0].spans()) == 1
    for export in exports:
        for resource in export["resource_attributes"]:
            assert resource["project_name"] == "firecrawl"
            assert resource["project_type"] == "observe"


@pytest.mark.parametrize("use_async", [False, True], ids=["sync", "async"])
@pytest.mark.parametrize("method", ["search", "start_crawl"])
@pytest.mark.parametrize("shape", ["flags", "explicit-mapping", "defaults"])
@pytest.mark.parametrize("failed", [False, True], ids=["success", "failure"])
def test_f6_scrapeformats_match_actual_payload(fake, telemetry, monkeypatch, use_async, method, shape, failed):
    from firecrawl.v2.types import ScrapeOptions, ScrapeFormats, JsonFormat

    if shape == "flags":
        formats = ScrapeFormats(markdown=False, html=True, images=True, json=True)
        expected = ["html"]  # SDK 4.46.2 does not serialize the images/json flags.
    elif shape == "explicit-mapping":
        formats = ScrapeFormats(formats=[JsonFormat(prompt=HTML, schema={"title": KEY}), "links"],
                                markdown=False, html=True, raw_html=True)
        expected = ["json", "links", "html", "rawHtml"]
    else:
        formats = ScrapeFormats(formats=["html"])
        expected = ["html", "markdown"]  # default markdown is actually serialized.
    scrape_options = {"formats": formats} if shape == "explicit-mapping" else ScrapeOptions(formats=formats)
    route = "/v2/search" if method == "search" else "/v2/crawl"
    _, response = fake.routes[("POST", route)]
    payloads = []
    def respond(body):
        payloads.append(body)
        return {"success": False, "error": HTML} if failed else response
    fake.routes[("POST", route)] = (400 if failed else 200, respond)
    observed = observe_vendor(monkeypatch, use_async, method)
    telemetry[2].instrument(tracer_provider=telemetry[1])
    if failed:
        with pytest.raises(Exception) as caught:
            invoke(fake, use_async, method=method, target="search" if method == "search" else PRIVATE_URL,
                   scrape_options=scrape_options)
        assert caught.value is observed[0]
    else:
        result = invoke(fake, use_async, method=method, target="search" if method == "search" else PRIVATE_URL,
                        scrape_options=scrape_options)
        assert result is observed[0]
    actual = payloads[0]["scrapeOptions"]["formats"]
    actual_names = [item if isinstance(item, str) else item["type"] for item in actual]
    assert list(dict.fromkeys(actual_names)) == expected
    spans, wire = exported(telemetry)
    assert len(spans) == 1 and format_names(spans[0]) == expected
    for secret in (HTML, KEY, PRIVATE_URL, PAGE_BODY):
        assert secret not in wire


@pytest.mark.parametrize("use_async", [False, True], ids=["sync", "async"])
@pytest.mark.parametrize("nested", [None, ["html"]], ids=["no-nested-formats", "nested-formats"])
def test_f6_scrape_options_override_convenience_formats(fake, telemetry, use_async, nested):
    from firecrawl.v2.types import ScrapeOptions

    payloads = []
    def respond(body):
        payloads.append(body)
        return {"success": True, "id": "review-job", "url": "unused"}
    fake.routes[("POST", "/v2/crawl")] = (200, respond)
    telemetry[2].instrument(tracer_provider=telemetry[1])
    invoke(fake, use_async, method="start_crawl", formats=["markdown"],
           scrape_options=ScrapeOptions(formats=nested))
    spans, _ = exported(telemetry)
    assert payloads[0].get("scrapeOptions", {}).get("formats") == nested
    assert format_names(spans[0]) == nested


@pytest.mark.parametrize("use_async", [False, True], ids=["sync", "async"])
def test_f3_default_query_uses_shared_session_context(fake, telemetry, monkeypatch, use_async):
    from fi_instrumentation.instrumentation import using_session
    from fi_instrumentation.fi_types import SpanAttributes

    observed = observe_vendor(monkeypatch, use_async, "search")
    telemetry[2].instrument(tracer_provider=telemetry[1], config=None)
    query = "find person@example.test"
    with using_session("review-session"):
        result = invoke(fake, use_async, method="search", target=query)
    assert result is observed[0]
    spans, _ = exported(telemetry)
    assert len(spans) == 1
    attributes = _flatten_attributes(spans[0]["attributes"])
    assert attributes["fi.retrieval.query"] == query
    assert attributes[SpanAttributes.GEN_AI_CONVERSATION_ID] == "review-session"


@pytest.mark.parametrize("use_async", [False, True], ids=["sync", "async"])
@pytest.mark.parametrize("policy", ["config", "env"])
@pytest.mark.parametrize("query", ["find person@example.test 415-555-2671",
                                   "x " * 507 + "boundary.person@example.test",
                                   "x " * 508 + "415-555-2671"],
                         ids=["ordinary", "email-boundary", "phone-boundary"])
def test_f3_query_pii_redaction_precedes_cap(fake, telemetry, monkeypatch, use_async, policy, query):
    from fi_instrumentation.instrumentation import TraceConfig
    from fi_instrumentation.instrumentation.pii_redaction import redact_pii_in_string

    if policy == "env":
        monkeypatch.setenv("FI_PII_REDACTION", "true")
        config = None
    else:
        config = TraceConfig(pii_redaction=True)
    telemetry[2].instrument(tracer_provider=telemetry[1], config=config)
    result = invoke(fake, use_async, method="search", target=query)
    assert len(result.web) == 2
    spans, wire = exported(telemetry)
    assert len(spans) == 1
    expected = redact_pii_in_string(query)[:1024]
    assert _flatten_attributes(spans[0]["attributes"])["fi.retrieval.query"] == expected
    for secret in ("person@example.test", "boundary.person", "415-555"):
        assert secret not in wire


@pytest.mark.parametrize("use_async", [False, True], ids=["sync", "async"])
@pytest.mark.parametrize("flag", ["hide_inputs", "hide_input_text"])
@pytest.mark.parametrize("policy", ["config", "env"])
def test_f3_hidden_inputs_omit_search_query(fake, telemetry, monkeypatch, use_async, flag, policy):
    from fi_instrumentation.instrumentation import TraceConfig

    if policy == "env":
        monkeypatch.setenv("FI_" + flag.upper(), "true")
        config = None
    else:
        config = TraceConfig(**{flag: True})
    telemetry[2].instrument(tracer_provider=telemetry[1], config=config)
    result = invoke(fake, use_async, method="search", target="QUERY-TO-HIDE-REVIEW")
    assert len(result.web) == 2
    spans, wire = exported(telemetry)
    assert len(spans) == 1
    assert "fi.retrieval.query" not in _flatten_attributes(spans[0]["attributes"])
    assert "QUERY-TO-HIDE-REVIEW" not in wire


@pytest.mark.parametrize("use_async", [False, True], ids=["sync", "async"])
@pytest.mark.parametrize("failed", [False, True], ids=["success", "failure"])
def test_f3_shared_suppression_emits_no_spans(fake, telemetry, monkeypatch, use_async, failed):
    from fi_instrumentation.instrumentation import suppress_tracing

    if failed:
        fake.routes[("POST", "/v2/search")] = (400, {"success": False, "error": HTML})
    observed = observe_vendor(monkeypatch, use_async, "search")
    telemetry[2].instrument(tracer_provider=telemetry[1])
    with suppress_tracing():
        if failed:
            with pytest.raises(Exception) as caught:
                invoke(fake, use_async, method="search", target="query")
            assert caught.value is observed[0]
        else:
            assert invoke(fake, use_async, method="search", target="query") is observed[0]
    assert fake.calls_to("POST", "/v2/search") == 1
    spans, _ = exported(telemetry)
    assert spans == []
    # Suppression and the local nested-call guard both reset for the next call.
    fake.routes[("POST", "/v2/search")] = (200, {"success": True, "data": {"web": []}})
    invoke(fake, use_async, method="search", target="after-suppression")
    spans, _ = exported(telemetry)
    assert [span["name"] for span in spans] == ["firecrawl.search"]


@pytest.mark.parametrize("invalid", [object(), {}], ids=["object", "dict"])
def test_f3_rejects_invalid_trace_config(telemetry, invalid):
    with pytest.raises(TypeError, match="TraceConfig"):
        telemetry[2].instrument(tracer_provider=telemetry[1], config=invalid)


@pytest.mark.parametrize("use_async", [False, True], ids=["sync", "async"])
def test_f3_key_discovery_failure_keeps_only_safe_metadata(fake, telemetry, use_async):
    from firecrawl import Firecrawl, AsyncFirecrawl
    from firecrawl.v2.types import ScrapeOptions

    class UnreadableConfig:
        @property
        def api_key(self):
            raise RuntimeError(HTML)

    telemetry[2].instrument(tracer_provider=telemetry[1])
    async def journey():
        client = AsyncFirecrawl(api_key=KEY, api_url=fake.origin, max_retries=1)
        client._v2_client.config = UnreadableConfig()
        return await client.search("query " + KEY, scrape_options=ScrapeOptions(formats=["html"]))
    if use_async:
        result = asyncio.run(journey())
    else:
        client = Firecrawl(api_key=KEY, api_url=fake.origin, max_retries=1)
        client._v2_client.config = UnreadableConfig()
        result = client.search("query " + KEY, scrape_options=ScrapeOptions(formats=["html"]))
    assert len(result.web) == 2
    spans, wire = exported(telemetry)
    attributes = _flatten_attributes(spans[0]["attributes"])
    assert "fi.retrieval.query" not in attributes
    assert int(attributes["fi.retrieval.document_count"]) == 2
    assert format_names(spans[0]) == ["html"]
    assert KEY not in wire and HTML not in wire


@pytest.mark.parametrize("use_async", [False, True], ids=["sync", "async"])
@pytest.mark.parametrize("groups,expected", [
    ({"web": 2}, 2), ({"news": 2}, 2), ({"images": 2}, 2),
    ({"web": 1, "news": 2, "images": 3}, 6), ({"web": 0, "news": 2}, 2),
    ({"web": 0, "news": 0, "images": 0, "tools": 0}, 0), ({}, 0),
    ({"tools": 2}, 2), ({"web": 1, "tools": 2}, 3),
], ids=["web", "news", "images", "mixed", "empty-web-news", "all-empty", "defaults-empty",
        "tools", "web-tools"])
def test_f4_counts_all_sdk_search_groups(fake, telemetry, monkeypatch, use_async, groups, expected):
    from firecrawl.v2.types import SearchData, DiscoveredTool

    data = {}
    for group, count in groups.items():
        record = ({"provider": "fixture", "capability": "search", "description": HTML}
                  if group == "tools" else {"url": PRIVATE_URL, "title": HTML})
        data[group] = [record.copy() for _ in range(count)]
    fake.routes[("POST", "/v2/search")] = (200, {"success": True, "data": data})
    observed = observe_vendor(monkeypatch, use_async, "search")
    telemetry[2].instrument(tracer_provider=telemetry[1])
    result = invoke(fake, use_async, method="search", target="search")
    assert result is observed[0] and isinstance(result, SearchData)
    assert sum(len(getattr(result, group) or []) for group in ("web", "news", "images", "tools")) == expected
    if result.tools:
        assert all(isinstance(item, DiscoveredTool) for item in result.tools)
    spans, wire = exported(telemetry)
    assert len(spans) == 1
    assert int(_flatten_attributes(spans[0]["attributes"])["fi.retrieval.document_count"]) == expected
    assert HTML not in wire and PRIVATE_URL not in wire


@pytest.mark.parametrize("use_async", [False, True], ids=["sync", "async"])
@pytest.mark.parametrize("shape,expected", [("unknown", None), ("malformed", None),
                                           ("data", 2), ("results", 2)])
def test_f4_unknown_counts_are_omitted_and_legacy_lists_survive(fake, telemetry, monkeypatch, use_async, shape, expected):
    from types import SimpleNamespace
    from firecrawl.v2.types import SearchData, Document

    # Unrecognized/malformed return shapes cannot come through validated normal
    # HTTP responses. Inject only these compatibility/error probes, using real
    # SDK models for list records and checking identity of the returned object.
    if shape == "unknown":
        result = object()
    elif shape == "malformed":
        result = SearchData.model_construct(news=HTML)
    else:
        result = SimpleNamespace(**{shape: [Document(markdown=HTML), Document(markdown=HTML)]})
    module = import_module("firecrawl.v2.client_async" if use_async else "firecrawl.v2.client")
    cls = getattr(module, "AsyncFirecrawlClient" if use_async else "FirecrawlClient")
    def search(*args, **kwargs):
        return result
    async def asearch(*args, **kwargs):
        return result
    monkeypatch.setattr(cls, "search", asearch if use_async else search)
    telemetry[2].instrument(tracer_provider=telemetry[1])
    assert invoke(fake, use_async, method="search", target="search") is result
    spans, wire = exported(telemetry)
    attributes = _flatten_attributes(spans[0]["attributes"])
    if expected is None:
        assert "fi.retrieval.document_count" not in attributes
    else:
        assert int(attributes["fi.retrieval.document_count"]) == expected
    assert HTML not in wire


@pytest.mark.parametrize("use_async", [False, True], ids=["sync", "async"])
def test_invalid_job_status_is_rejected_without_exporting_validation_input(fake, telemetry, monkeypatch, use_async):
    from pydantic import ValidationError

    sentinel = " ".join((KEY, PRIVATE_URL, HTML))
    fake.routes[("GET", "/v2/crawl/review-job")] = (
        200, {"success": True, "status": sentinel, "total": 1, "completed": 0,
              "creditsUsed": 0, "data": []},
    )
    observed = observe_vendor(monkeypatch, use_async, "get_crawl_status")
    telemetry[2].instrument(tracer_provider=telemetry[1])
    with pytest.raises(ValidationError) as caught:
        invoke(fake, use_async, method="get_crawl_status", target="review-job")
    assert caught.value is observed[0] and caught.value.errors()[0]["input"] == sentinel
    spans, wire = exported(telemetry)
    assert len(spans) == 1 and spans[0]["status"]["code"] == "STATUS_CODE_ERROR"
    assert len(spans[0]["events"]) == 1
    assert "firecrawl.status" not in _flatten_attributes(spans[0]["attributes"])
    for secret in (KEY, PRIVATE_URL, HTML):
        assert secret not in wire
