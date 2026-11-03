"""Isolated provider and reversible instrumentation for each test."""

import importlib
import inspect

import pytest
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from traceai_lancedb import LanceDBInstrumentor


@pytest.fixture
def telemetry(monkeypatch):
    # Restore even the old, broken implementation during the regression red run.
    for name in ("db", "table", "query", "remote.db", "remote.table"):
        module = importlib.import_module("lancedb." + name)
        for cls in vars(module).values():
            if inspect.isclass(cls) and cls.__module__ == module.__name__:
                for method, original in list(vars(cls).items()):
                    if callable(original) and not method.startswith("__"):
                        monkeypatch.setattr(cls, method, original)
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    instrumentor = LanceDBInstrumentor()
    instrumentor.instrument(tracer_provider=provider)
    try:
        yield exporter, provider, instrumentor
    finally:
        instrumentor.uninstrument()
        provider.shutdown()
