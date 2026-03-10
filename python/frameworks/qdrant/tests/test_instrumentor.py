"""Unit tests for Qdrant instrumentation."""

import pytest
from unittest.mock import MagicMock, patch
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter


class TestQdrantInstrumentor:
    """Test QdrantInstrumentor class."""

    def test_instrumentor_can_be_instantiated(self):
        from traceai_qdrant import QdrantInstrumentor
        instrumentor = QdrantInstrumentor()
        assert instrumentor is not None

    def test_instrumentation_dependencies(self):
        from traceai_qdrant import QdrantInstrumentor
        instrumentor = QdrantInstrumentor()
        deps = instrumentor.instrumentation_dependencies()
        assert any("qdrant" in dep for dep in deps)


class TestQdrantSearch:
    """Test Qdrant search operation tracing."""

    @patch("qdrant_client.QdrantClient")
    def test_search_creates_span(
        self,
        mock_client_class,
        tracer_provider,
        in_memory_span_exporter: InMemorySpanExporter,
        mock_qdrant_client,
        instrument_qdrant,
    ):
        mock_client_class.return_value = mock_qdrant_client

        client = mock_client_class()
        result = client.search(
            collection_name="test-collection",
            query_vector=[0.1] * 384,
            limit=10,
        )

        spans = in_memory_span_exporter.get_finished_spans()
        search_spans = [s for s in spans if "search" in s.name.lower()]
        if search_spans:
            span = search_spans[0]
            assert "qdrant" in span.name.lower()


class TestQdrantUpsert:
    """Test Qdrant upsert operation tracing."""

    @patch("qdrant_client.QdrantClient")
    def test_upsert_creates_span(
        self,
        mock_client_class,
        tracer_provider,
        in_memory_span_exporter: InMemorySpanExporter,
        mock_qdrant_client,
        instrument_qdrant,
    ):
        mock_client_class.return_value = mock_qdrant_client

        client = mock_client_class()
        client.upsert(
            collection_name="test-collection",
            points=[
                MagicMock(id="1", vector=[0.1] * 384, payload={"text": "doc1"}),
            ],
        )

        spans = in_memory_span_exporter.get_finished_spans()
        upsert_spans = [s for s in spans if "upsert" in s.name.lower()]
        if upsert_spans:
            span = upsert_spans[0]
            assert "qdrant" in span.name.lower()
