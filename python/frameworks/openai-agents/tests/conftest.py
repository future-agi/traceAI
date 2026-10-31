from typing import Iterator

import pytest
from traceai_openai_agents import OpenAIAgentsInstrumentor
from agents import set_trace_processors, set_tracing_disabled

from opentelemetry import trace as trace_api
from opentelemetry.sdk import trace as trace_sdk
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter


@pytest.fixture
def in_memory_span_exporter() -> InMemorySpanExporter:
    return InMemorySpanExporter()


@pytest.fixture
def tracer_provider(
    in_memory_span_exporter: InMemorySpanExporter,
) -> trace_api.TracerProvider:
    tracer_provider = trace_sdk.TracerProvider()
    span_processor = SimpleSpanProcessor(span_exporter=in_memory_span_exporter)
    tracer_provider.add_span_processor(span_processor=span_processor)
    return tracer_provider


@pytest.fixture
def instrument(
    tracer_provider: trace_api.TracerProvider,
    in_memory_span_exporter: InMemorySpanExporter,
) -> Iterator[None]:
    set_tracing_disabled(False)
    OpenAIAgentsInstrumentor().instrument(tracer_provider=tracer_provider)
    try:
        yield
    finally:
        # The SDK processor list is global. Never leave a vendor exporter or our
        # processor installed after an offline test.
        set_trace_processors([])
        OpenAIAgentsInstrumentor().uninstrument()
