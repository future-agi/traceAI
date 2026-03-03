"""Pytest configuration and fixtures for Pinecone instrumentation tests."""

import pytest
from typing import Generator
from unittest.mock import MagicMock, patch

from opentelemetry import trace as trace_api
from opentelemetry.sdk import trace as trace_sdk
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter


@pytest.fixture
def in_memory_span_exporter() -> InMemorySpanExporter:
    """Create an in-memory span exporter for testing."""
    return InMemorySpanExporter()


@pytest.fixture
def tracer_provider(
    in_memory_span_exporter: InMemorySpanExporter,
) -> trace_api.TracerProvider:
    """Create a tracer provider with in-memory exporter."""
    resource = Resource(attributes={"service.name": "test-pinecone"})
    tracer_provider = trace_sdk.TracerProvider(resource=resource)
    span_processor = SimpleSpanProcessor(span_exporter=in_memory_span_exporter)
    tracer_provider.add_span_processor(span_processor=span_processor)
    return tracer_provider


@pytest.fixture
def mock_pinecone_index():
    """Create a mock Pinecone index."""
    mock_index = MagicMock()

    # Mock query response
    mock_index.query.return_value = MagicMock(
        matches=[
            MagicMock(id="vec1", score=0.95, metadata={"text": "doc1"}),
            MagicMock(id="vec2", score=0.87, metadata={"text": "doc2"}),
        ]
    )

    # Mock upsert response
    mock_index.upsert.return_value = MagicMock(upserted_count=10)

    # Mock delete response
    mock_index.delete.return_value = {}

    # Mock fetch response
    mock_index.fetch.return_value = MagicMock(
        vectors={
            "vec1": MagicMock(id="vec1", values=[0.1] * 1536),
        }
    )

    # Mock update response
    mock_index.update.return_value = {}

    # Mock describe_index_stats response
    mock_index.describe_index_stats.return_value = MagicMock(
        total_vector_count=1000,
        dimension=1536,
    )

    return mock_index


@pytest.fixture
def instrument_pinecone(
    tracer_provider: trace_api.TracerProvider,
    in_memory_span_exporter: InMemorySpanExporter,
) -> Generator[None, None, None]:
    """Instrument Pinecone for testing."""
    from traceai_pinecone import PineconeInstrumentor

    instrumentor = PineconeInstrumentor()
    instrumentor.instrument(tracer_provider=tracer_provider)
    yield
    instrumentor.uninstrument()
    in_memory_span_exporter.clear()
