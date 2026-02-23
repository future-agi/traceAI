"""Pytest configuration and fixtures for ChromaDB instrumentation tests."""

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
    resource = Resource(attributes={"service.name": "test-chromadb"})
    tracer_provider = trace_sdk.TracerProvider(resource=resource)
    span_processor = SimpleSpanProcessor(span_exporter=in_memory_span_exporter)
    tracer_provider.add_span_processor(span_processor=span_processor)
    return tracer_provider


@pytest.fixture
def mock_chroma_collection():
    """Create a mock ChromaDB collection."""
    mock_collection = MagicMock()
    mock_collection.name = "test-collection"

    # Mock query response
    mock_collection.query.return_value = {
        "ids": [["id1", "id2"]],
        "documents": [["doc1", "doc2"]],
        "distances": [[0.1, 0.2]],
        "metadatas": [[{"key": "val1"}, {"key": "val2"}]],
    }

    # Mock add response
    mock_collection.add.return_value = None

    # Mock get response
    mock_collection.get.return_value = {
        "ids": ["id1"],
        "documents": ["doc1"],
        "metadatas": [{"key": "val1"}],
    }

    # Mock count response
    mock_collection.count.return_value = 100

    return mock_collection


@pytest.fixture
def instrument_chromadb(
    tracer_provider: trace_api.TracerProvider,
    in_memory_span_exporter: InMemorySpanExporter,
) -> Generator[None, None, None]:
    """Instrument ChromaDB for testing."""
    from traceai_chromadb import ChromaDBInstrumentor

    instrumentor = ChromaDBInstrumentor()
    instrumentor.instrument(tracer_provider=tracer_provider)
    yield
    instrumentor.uninstrument()
    in_memory_span_exporter.clear()
