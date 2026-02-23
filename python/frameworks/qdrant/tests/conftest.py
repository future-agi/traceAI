"""Pytest configuration and fixtures for Qdrant instrumentation tests."""

import pytest
from typing import Generator
from unittest.mock import MagicMock

from opentelemetry import trace as trace_api
from opentelemetry.sdk import trace as trace_sdk
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter


@pytest.fixture
def in_memory_span_exporter() -> InMemorySpanExporter:
    return InMemorySpanExporter()


@pytest.fixture
def tracer_provider(in_memory_span_exporter: InMemorySpanExporter) -> trace_api.TracerProvider:
    resource = Resource(attributes={"service.name": "test-qdrant"})
    tracer_provider = trace_sdk.TracerProvider(resource=resource)
    tracer_provider.add_span_processor(SimpleSpanProcessor(in_memory_span_exporter))
    return tracer_provider


@pytest.fixture
def mock_qdrant_client():
    """Create a mock Qdrant client."""
    mock_client = MagicMock()

    # Mock search response
    mock_client.search.return_value = [
        MagicMock(id="point1", score=0.95, payload={"text": "doc1"}),
        MagicMock(id="point2", score=0.87, payload={"text": "doc2"}),
    ]

    # Mock upsert response
    mock_client.upsert.return_value = MagicMock(status="completed")

    # Mock delete response
    mock_client.delete.return_value = MagicMock(status="completed")

    # Mock retrieve response
    mock_client.retrieve.return_value = [
        MagicMock(id="point1", payload={"text": "doc1"}),
    ]

    # Mock count response
    mock_client.count.return_value = MagicMock(count=100)

    return mock_client


@pytest.fixture
def instrument_qdrant(
    tracer_provider: trace_api.TracerProvider,
    in_memory_span_exporter: InMemorySpanExporter,
) -> Generator[None, None, None]:
    from traceai_qdrant import QdrantInstrumentor

    instrumentor = QdrantInstrumentor()
    instrumentor.instrument(tracer_provider=tracer_provider)
    yield
    instrumentor.uninstrument()
    in_memory_span_exporter.clear()
