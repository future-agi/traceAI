"""Separate collection work is not duplicate instrumentation."""

import asyncio
import inspect
from uuid import uuid4

import chromadb
import pytest
from chromadb.config import Settings


@pytest.mark.parametrize(
    "operation,same_collection", [("count", False), ("get", False), ("get", True)]
)
def test_distinct_nested_operation(
    collection,
    other_collection,
    instrument_chromadb,
    in_memory_span_exporter,
    monkeypatch,
    operation,
    same_collection,
):
    cls = type(collection)
    # A second instance with the same collection name must remain distinguishable.
    asynchronous = inspect.iscoroutinefunction(cls.count)
    other = collection if same_collection else other_collection
    if asynchronous:

        async def count(**kwargs):
            await getattr(other, operation)()
            return 2
    else:

        def count(**kwargs):
            getattr(other, operation)()
            return 2

    monkeypatch.setattr(collection._client, "_count", count)
    result = collection.count()
    if asynchronous:
        result = asyncio.run(result)
    assert result == 2
    spans = in_memory_span_exporter.get_finished_spans()
    assert [span.name for span in spans] == [f"chroma {operation}", "chroma count"]
    assert spans[0].parent.span_id == spans[1].context.span_id


def test_real_embedding_cache_is_traced(
    instrument_chromadb, tracer_provider, in_memory_span_exporter
):
    client = chromadb.EphemeralClient(settings=Settings(anonymized_telemetry=False))
    cache = client.create_collection(f"cache-{uuid4().hex}", embedding_function=None)
    cache.add(ids=["cached-vector"], embeddings=[[1.0, 0.0, 0.0]])

    class CachedEmbedding:
        def name(self):
            return "synthetic-cached-embedding"

        def get_config(self):
            return {}

        def __call__(self, input):
            with tracer_provider.get_tracer("test").start_as_current_span("embedding"):
                values = cache.get(ids=["cached-vector"], include=["embeddings"])[
                    "embeddings"
                ]
                return [values[0] for _ in input]

    destination = client.create_collection(
        f"destination-{uuid4().hex}", embedding_function=CachedEmbedding()
    )
    try:
        in_memory_span_exporter.clear()
        destination.add(ids=["doc"], documents=["synthetic-document"])
        spans = in_memory_span_exporter.get_finished_spans()
        relevant = [
            span
            for span in spans
            if span.name in ("chroma get", "embedding", "chroma add")
        ]
        assert [span.name for span in relevant] == [
            "chroma get",
            "embedding",
            "chroma add",
        ]
        assert relevant[0].attributes["db.namespace"] == cache.name
        assert relevant[0].parent.span_id == relevant[1].context.span_id
        assert relevant[1].parent.span_id == relevant[2].context.span_id
    finally:
        client.delete_collection(destination.name)
        client.delete_collection(cache.name)
