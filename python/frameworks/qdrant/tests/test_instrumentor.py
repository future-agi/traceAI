"""Offline contracts using the real Qdrant in-memory client."""

import pytest
from opentelemetry.trace import StatusCode
from qdrant_client import QdrantClient, models

from traceai_qdrant import QdrantInstrumentor


@pytest.fixture
def client(instrument_qdrant):
    client = QdrantClient(":memory:")
    client.create_collection(
        "documents", vectors_config=models.VectorParams(size=3, distance=models.Distance.COSINE)
    )
    yield client
    client.close()


def insert(client):
    return client.upsert(
        collection_name="documents",
        points=[models.PointStruct(id=1, vector=[1.0, 0.0, 0.0], payload={"text": "document"})],
    )


class TestQdrantInstrumentor:
    def test_instrumentor_can_be_instantiated(self):
        assert isinstance(QdrantInstrumentor(), QdrantInstrumentor)

    def test_instrumentation_dependencies(self):
        assert tuple(QdrantInstrumentor().instrumentation_dependencies()) == ("qdrant-client >= 1.6.0",)

    def test_uninstrument_restores_available_methods(self, tracer_provider):
        names = ("query", "query_points", "search", "upsert", "delete", "retrieve", "scroll", "count")
        originals = {name: getattr(QdrantClient, name, None) for name in names}
        instrumentor = QdrantInstrumentor()
        instrumentor.instrument(tracer_provider=tracer_provider)
        try:
            for name, original in originals.items():
                if original is not None:
                    assert getattr(QdrantClient, name) is not original
                else:
                    assert not hasattr(QdrantClient, name)
        finally:
            instrumentor.uninstrument()
        for name, original in originals.items():
            assert getattr(QdrantClient, name, None) is original


class TestQdrantSearch:
    def test_search_creates_span(self, client, in_memory_span_exporter):
        insert(client)
        in_memory_span_exporter.clear()
        # Exercise the actual dense-vector API in both pre-1.10 and current SDKs.
        if hasattr(client, "query_points"):
            points = client.query_points("documents", query=[1.0, 0.0, 0.0], limit=1).points
        else:
            points = client.search("documents", query_vector=[1.0, 0.0, 0.0], limit=1)
        assert len(points) == 1
        assert points[0].id == 1
        assert points[0].payload == {"text": "document"}
        span, = in_memory_span_exporter.get_finished_spans()
        assert span.name == "qdrant query"
        assert span.status.status_code == StatusCode.OK
        assert span.attributes["db.system"] == "qdrant"
        assert span.attributes["db.namespace"] == "documents"
        assert span.attributes["db.vector.query.top_k"] == 1
        assert span.attributes["db.vector.results.count"] == 1


class TestQdrantUpsert:
    def test_upsert_creates_span(self, client, in_memory_span_exporter):
        result = insert(client)
        assert result.status == models.UpdateStatus.COMPLETED
        span, = in_memory_span_exporter.get_finished_spans()
        assert span.name == "qdrant upsert"
        assert span.attributes["db.vector.upsert.count"] == 1
        assert span.status.status_code == StatusCode.OK

    def test_error_propagates_and_finishes_span(self, client, in_memory_span_exporter):
        with pytest.raises(ValueError, match="not found"):
            client.upsert("missing", points=[models.PointStruct(id=1, vector=[1.0, 0.0, 0.0])])
        span, = in_memory_span_exporter.get_finished_spans()
        assert span.status.status_code == StatusCode.ERROR
        assert span.events[0].name == "exception"


def test_crud_results_and_spans(client, in_memory_span_exporter):
    insert(client)
    in_memory_span_exporter.clear()
    assert client.count("documents").count == 1
    assert client.retrieve("documents", ids=[1])[0].payload == {"text": "document"}
    records, _ = client.scroll("documents", limit=1)
    assert records[0].id == 1
    assert client.delete("documents", points_selector=models.PointIdsList(points=[1])).status == models.UpdateStatus.COMPLETED
    spans = in_memory_span_exporter.get_finished_spans()
    assert [span.name for span in spans] == ["qdrant count", "qdrant retrieve", "qdrant scroll", "qdrant delete"]
    assert all(span.status.status_code == StatusCode.OK for span in spans)
    assert all(span.attributes["db.namespace"] == "documents" for span in spans)
