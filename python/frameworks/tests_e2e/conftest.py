"""
Pytest configuration for E2E integration tests.

These tests require real database instances. Use the provided docker-compose.yml
to spin up test databases.
"""

import pytest
from typing import Generator

from opentelemetry import trace as trace_api
from opentelemetry.sdk import trace as trace_sdk
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter


def pytest_configure(config):
    """Register custom markers."""
    config.addinivalue_line(
        "markers", "integration: mark test as integration test (requires real DB)"
    )
    config.addinivalue_line(
        "markers", "chromadb: mark test as requiring ChromaDB"
    )
    config.addinivalue_line(
        "markers", "qdrant: mark test as requiring Qdrant"
    )
    config.addinivalue_line(
        "markers", "milvus: mark test as requiring Milvus"
    )
    config.addinivalue_line(
        "markers", "weaviate: mark test as requiring Weaviate"
    )
    config.addinivalue_line(
        "markers", "lancedb: mark test as requiring LanceDB"
    )
    config.addinivalue_line(
        "markers", "pgvector: mark test as requiring PostgreSQL with pgvector"
    )
    config.addinivalue_line(
        "markers", "redis: mark test as requiring Redis with RediSearch"
    )


@pytest.fixture
def in_memory_span_exporter() -> InMemorySpanExporter:
    """Create an in-memory span exporter for testing."""
    return InMemorySpanExporter()


@pytest.fixture
def tracer_provider(
    in_memory_span_exporter: InMemorySpanExporter,
) -> trace_api.TracerProvider:
    """Create a tracer provider with in-memory exporter."""
    resource = Resource(attributes={"service.name": "e2e-test"})
    tracer_provider = trace_sdk.TracerProvider(resource=resource)
    span_processor = SimpleSpanProcessor(span_exporter=in_memory_span_exporter)
    tracer_provider.add_span_processor(span_processor=span_processor)
    return tracer_provider


def generate_test_embedding(text: str, dimension: int = 384) -> list[float]:
    """Generate deterministic test embedding from text."""
    import hashlib
    hash_bytes = hashlib.sha256(text.encode()).digest()
    embedding = []
    for i in range(dimension):
        byte_val = hash_bytes[i % len(hash_bytes)]
        embedding.append((byte_val / 255.0) * 2 - 1)
    return embedding
