"""Pytest configuration and fixtures for Exa instrumentation tests."""

from typing import Generator

import pytest
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
    """Create a tracer provider with an in-memory exporter."""
    resource = Resource(attributes={"service.name": "test-exa"})
    provider = trace_sdk.TracerProvider(resource=resource)
    provider.add_span_processor(SimpleSpanProcessor(in_memory_span_exporter))
    return provider
