"""Unit tests for Exa instrumentation."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any, Dict

import pytest
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from opentelemetry.trace import StatusCode

pytest.importorskip("exa_py", reason="exa-py must be installed to test its instrumentor")
from exa_py.api import Exa  # noqa: E402
from traceai_exa import ExaInstrumentor  # noqa: E402

_API_KEY = "exa-api-key-should-not-appear-in-a-span"


def _tracing() -> tuple[TracerProvider, InMemorySpanExporter]:
    exporter = InMemorySpanExporter()
    provider = TracerProvider(resource=Resource.create({"service.name": "test-exa"}))
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    return provider, exporter


def _instrument(provider: TracerProvider) -> ExaInstrumentor:
    instrumentor = ExaInstrumentor()
    instrumentor.instrument(tracer_provider=provider)
    return instrumentor


def _attrs(exporter: InMemorySpanExporter) -> Dict[str, Any]:
    spans = exporter.get_finished_spans()
    assert len(spans) == 1
    return dict(spans[0].attributes or {})


def test_search_records_retrieval_contract(monkeypatch: pytest.MonkeyPatch) -> None:
    def search(_self: Exa, _query: str, **_kwargs: Any) -> Any:
        return SimpleNamespace(results=[SimpleNamespace(), SimpleNamespace()])

    monkeypatch.setattr(Exa, "search", search)
    provider, exporter = _tracing()
    instrumentor = _instrument(provider)
    try:
        Exa(api_key=_API_KEY).search("retrieval query")
    finally:
        instrumentor.uninstrument()

    spans = exporter.get_finished_spans()
    assert len(spans) == 1
    span = spans[0]
    attrs = dict(span.attributes or {})
    assert span.name == "exa.search"
    assert attrs["fi.span.kind"] == "RETRIEVER"
    assert attrs["fi.retrieval.query"] == "retrieval query"
    assert attrs["fi.retrieval.document_count"] == 2
    assert all(_API_KEY not in str(value) for value in attrs.values())


def test_search_exception_is_recorded_and_reraised(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def search(_self: Exa, _query: str, **_kwargs: Any) -> Any:
        raise RuntimeError("Exa is unavailable")

    monkeypatch.setattr(Exa, "search", search)
    provider, exporter = _tracing()
    instrumentor = _instrument(provider)
    try:
        with pytest.raises(RuntimeError, match="Exa is unavailable"):
            Exa(api_key=_API_KEY).search("retrieval query")
    finally:
        instrumentor.uninstrument()

    span = exporter.get_finished_spans()[0]
    assert span.status.status_code is StatusCode.ERROR
    assert any(event.name == "exception" for event in span.events)


def test_search_and_contents_uses_search_span_name(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def search_and_contents(_self: Exa, _query: str, **_kwargs: Any) -> Any:
        return SimpleNamespace(results=[])

    monkeypatch.setattr(Exa, "search_and_contents", search_and_contents)
    provider, exporter = _tracing()
    instrumentor = _instrument(provider)
    try:
        Exa(api_key=_API_KEY).search_and_contents("deprecated alias")
    finally:
        instrumentor.uninstrument()

    assert exporter.get_finished_spans()[0].name == "exa.search"


def test_answer_has_no_model_attribute(monkeypatch: pytest.MonkeyPatch) -> None:
    def answer(_self: Exa, _query: str, **_kwargs: Any) -> Any:
        return SimpleNamespace(answer="answer", citations=[])

    monkeypatch.setattr(Exa, "answer", answer)
    provider, exporter = _tracing()
    instrumentor = _instrument(provider)
    try:
        Exa(api_key=_API_KEY).answer("answer query", model="exa")
    finally:
        instrumentor.uninstrument()

    spans = exporter.get_finished_spans()
    assert spans[0].name == "exa.answer"
    assert not any("model" in key for key in _attrs(exporter))
