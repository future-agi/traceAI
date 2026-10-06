"""Unit tests for Firecrawl instrumentation."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from opentelemetry.trace import StatusCode

pytest.importorskip("firecrawl", reason="firecrawl-py must be installed to test its instrumentor")
from firecrawl.v2.client import FirecrawlClient  # noqa: E402
from traceai_firecrawl import FirecrawlInstrumentor  # noqa: E402

_API_KEY = "fc-api-key-should-not-appear-in-a-span"


def _tracing() -> tuple[TracerProvider, InMemorySpanExporter]:
    exporter = InMemorySpanExporter()
    provider = TracerProvider(resource=Resource.create({"service.name": "test-firecrawl"}))
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    return provider, exporter


def _instrument(provider: TracerProvider) -> FirecrawlInstrumentor:
    instrumentor = FirecrawlInstrumentor()
    instrumentor.instrument(tracer_provider=provider)
    return instrumentor


def test_crawl_emits_one_span_for_three_pages(monkeypatch: pytest.MonkeyPatch) -> None:
    pages = [SimpleNamespace(), SimpleNamespace(), SimpleNamespace()]

    def crawl(_self: FirecrawlClient, _url: str, **_kwargs: Any) -> Any:
        return SimpleNamespace(id="job-1", data=pages, credits_used=3)

    monkeypatch.setattr(FirecrawlClient, "crawl", crawl)
    provider, exporter = _tracing()
    instrumentor = _instrument(provider)
    try:
        FirecrawlClient(api_key=_API_KEY).crawl("https://example.com", limit=10)
    finally:
        instrumentor.uninstrument()

    spans = exporter.get_finished_spans()
    assert len(spans) == 1
    span = spans[0]
    attrs = dict(span.attributes or {})
    assert span.name == "firecrawl.crawl"
    assert attrs["fi.span.kind"] == "TOOL"
    assert attrs["firecrawl.page_count"] == 3
    assert attrs["firecrawl.limit"] == 10
    assert attrs["firecrawl.job_id"] == "job-1"
    assert attrs["server.address"] == "example.com"
    assert all(_API_KEY not in str(value) for value in attrs.values())
    assert all(not key.startswith("fc-") for key in attrs)


def test_scrape_exception_is_recorded_and_reraised(monkeypatch: pytest.MonkeyPatch) -> None:
    def scrape(_self: FirecrawlClient, _url: str, **_kwargs: Any) -> Any:
        raise RuntimeError("Firecrawl is unavailable")

    monkeypatch.setattr(FirecrawlClient, "scrape", scrape)
    provider, exporter = _tracing()
    instrumentor = _instrument(provider)
    try:
        with pytest.raises(RuntimeError, match="Firecrawl is unavailable"):
            FirecrawlClient(api_key=_API_KEY).scrape("https://example.com")
    finally:
        instrumentor.uninstrument()

    span = exporter.get_finished_spans()[0]
    assert span.status.status_code is StatusCode.ERROR
    assert any(event.name == "exception" for event in span.events)


def test_search_records_query_and_count(monkeypatch: pytest.MonkeyPatch) -> None:
    def search(_self: FirecrawlClient, _query: str, **_kwargs: Any) -> Any:
        return SimpleNamespace(web=[SimpleNamespace(), SimpleNamespace()])

    monkeypatch.setattr(FirecrawlClient, "search", search)
    provider, exporter = _tracing()
    instrumentor = _instrument(provider)
    try:
        FirecrawlClient(api_key=_API_KEY).search("retrieval query")
    finally:
        instrumentor.uninstrument()

    spans = exporter.get_finished_spans()
    assert len(spans) == 1
    attrs = dict(spans[0].attributes or {})
    assert spans[0].name == "firecrawl.search"
    assert attrs["fi.retrieval.query"] == "retrieval query"
    assert attrs["fi.retrieval.document_count"] == 2
    assert all(_API_KEY not in str(value) for value in attrs.values())
