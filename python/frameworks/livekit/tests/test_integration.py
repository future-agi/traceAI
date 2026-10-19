"""
Tests for traceai-livekit: enabling the Future AGI attribute mapping.

livekit-agents 1.8 adds a privacy filter to the tracer provider it is handed, through
`add_span_processor`. `fi_instrumentation.register()` marks its own exporter as a default that the
next `add_span_processor` call replaces, so the filter used to shut the exporter down and no trace
was sent. These tests pin that the exporter stays next to LiveKit's filter.

No network and no real Future AGI keys: exports are captured in memory, and the provider
`register()` returns is never installed as the process-wide OpenTelemetry provider.
"""

import signal
from typing import Any, Iterator, List

import pytest
from opentelemetry.sdk.trace.export import SimpleSpanProcessor, SpanExportResult
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

pytest.importorskip("livekit.agents", minversion="1.8")

from fi_instrumentation.otel import (  # noqa: E402
    BatchSpanProcessor,
    Transport,
    register,
)
from livekit.agents.telemetry import pii  # noqa: E402

from traceai_livekit import (  # noqa: E402
    MappedGRPCSpanExporter,
    MappedHTTPSpanExporter,
    enable_grpc_attribute_mapping,
    enable_http_attribute_mapping,
)


@pytest.fixture()
def exported_spans(monkeypatch: pytest.MonkeyPatch) -> List[Any]:
    """Spans handed to the Future AGI exporters, captured instead of sent."""
    captured: List[Any] = []

    def fake_export(self: Any, spans: Any) -> SpanExportResult:
        captured.extend(spans)
        return SpanExportResult.SUCCESS

    monkeypatch.setattr(MappedHTTPSpanExporter, "export", fake_export)
    monkeypatch.setattr(MappedGRPCSpanExporter, "export", fake_export)
    return captured


@pytest.fixture()
def make_provider(monkeypatch: pytest.MonkeyPatch) -> Iterator[Any]:
    """Build a provider with `register()` and make the integration pick it up.

    The provider is returned to the integration through a patched
    `trace_api.get_tracer_provider`, so the process-wide provider is never set. `register()`
    replaces the SIGINT/SIGTERM handlers; they are put back, and every provider is shut down.
    """
    monkeypatch.setenv("FI_API_KEY", "test-api-key")
    monkeypatch.setenv("FI_SECRET_KEY", "test-secret-key")
    saved_handlers = {
        sig: signal.getsignal(sig) for sig in (signal.SIGINT, signal.SIGTERM)
    }
    providers: List[Any] = []

    def _make(transport: Transport = Transport.HTTP) -> Any:
        provider = register(
            project_name="livekit-test",
            set_global_tracer_provider=False,
            verbose=False,
            transport=transport,
        )
        providers.append(provider)
        monkeypatch.setattr(
            "traceai_livekit.integration.trace_api.get_tracer_provider",
            lambda: provider,
        )
        return provider

    yield _make

    for provider in providers:
        provider.shutdown()
    for sig, handler in saved_handlers.items():
        signal.signal(sig, handler)


def _processors(provider: Any) -> List[Any]:
    return list(provider._active_span_processor._span_processors)


def _future_agi_processors(provider: Any, exporter_type: type) -> List[Any]:
    return [
        p
        for p in _processors(provider)
        if isinstance(getattr(p, "span_exporter", None), exporter_type)
    ]


def test_enabling_http_mapping_keeps_the_exporter_alongside_livekits_privacy_filter(
    make_provider: Any,
) -> None:
    provider = make_provider()

    assert enable_http_attribute_mapping() is True

    processors = _processors(provider)
    assert len(processors) == 2
    assert isinstance(processors[0], pii._PIIFilteringSpanProcessor)
    assert isinstance(processors[1], BatchSpanProcessor)
    assert isinstance(processors[1].span_exporter, MappedHTTPSpanExporter)


def test_a_span_ended_after_enabling_reaches_the_future_agi_exporter(
    make_provider: Any, exported_spans: List[Any]
) -> None:
    provider = make_provider()
    enable_http_attribute_mapping()

    span = provider.get_tracer("t").start_span("livekit-turn")
    span.end()
    provider.force_flush()

    assert [s.name for s in exported_spans] == ["livekit-turn"]


def test_enabling_grpc_mapping_keeps_the_exporter(
    make_provider: Any, exported_spans: List[Any]
) -> None:
    pytest.importorskip("grpc")
    provider = make_provider(Transport.GRPC)

    assert enable_grpc_attribute_mapping() is True

    processors = _processors(provider)
    assert any(isinstance(p, pii._PIIFilteringSpanProcessor) for p in processors)
    assert len(_future_agi_processors(provider, MappedGRPCSpanExporter)) == 1


def test_the_exporter_is_kept_even_when_no_exporter_was_swapped(
    make_provider: Any, exported_spans: List[Any]
) -> None:
    # An HTTP provider with the gRPC mapping: nothing is swapped, and the exporter still stays.
    provider = make_provider(Transport.HTTP)

    assert enable_grpc_attribute_mapping() is False

    processors = _processors(provider)
    assert isinstance(processors[0], pii._PIIFilteringSpanProcessor)
    assert any(isinstance(p, BatchSpanProcessor) for p in processors)


def test_a_processor_added_after_enabling_runs_alongside_the_exporter(
    make_provider: Any, exported_spans: List[Any]
) -> None:
    provider = make_provider()
    enable_http_attribute_mapping()
    other = InMemorySpanExporter()
    other_processor = SimpleSpanProcessor(other)

    provider.add_span_processor(other_processor)

    processors = _processors(provider)
    assert other_processor in processors
    assert len(_future_agi_processors(provider, MappedHTTPSpanExporter)) == 1

    provider.get_tracer("t").start_span("after-add").end()
    provider.force_flush()

    assert [s.name for s in exported_spans] == ["after-add"]
    assert [s.name for s in other.get_finished_spans()] == ["after-add"]


def test_register_alone_still_lets_add_span_processor_replace_its_default(
    make_provider: Any,
) -> None:
    provider = make_provider()
    other_processor = SimpleSpanProcessor(InMemorySpanExporter())

    provider.add_span_processor(other_processor)

    assert _processors(provider) == [other_processor]
