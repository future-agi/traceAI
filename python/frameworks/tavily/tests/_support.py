"""In-memory tracing helpers for the real-SDK tests.

Spans go to a plain SDK ``TracerProvider`` with an in-memory exporter, never
through ``fi_instrumentation.register()``: register()'s processor turns UNSET
into OK before export, so only an in-memory provider shows the wrapper's own
status.
"""

from __future__ import annotations

import contextlib
from dataclasses import dataclass
from typing import Any, Dict, Iterator, List

from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import ReadableSpan, TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter


@dataclass
class Traced:
    exporter: InMemorySpanExporter
    provider: TracerProvider

    def spans(self) -> List[ReadableSpan]:
        return list(self.exporter.get_finished_spans())

    def one(self) -> ReadableSpan:
        spans = self.spans()
        assert len(spans) == 1, [span.name for span in spans]
        return spans[0]

    def wire(self) -> str:
        """Everything an exporter could send: attributes, events, status."""
        return "".join(span.to_json() for span in self.spans())


def new_provider() -> Traced:
    exporter = InMemorySpanExporter()
    provider = TracerProvider(resource=Resource.create({"service.name": "test-tavily"}))
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    return Traced(exporter, provider)


@contextlib.contextmanager
def instrumented(**options: Any) -> Iterator[Traced]:
    """Instrument Tavily against a fresh in-memory provider; uninstrument after."""
    from traceai_tavily import TavilyInstrumentor

    traced = new_provider()
    instrumentor = TavilyInstrumentor()
    instrumentor.instrument(tracer_provider=traced.provider, **options)
    try:
        yield traced
    finally:
        instrumentor.uninstrument()


def attrs(span: ReadableSpan) -> Dict[str, Any]:
    return dict(span.attributes or {})
