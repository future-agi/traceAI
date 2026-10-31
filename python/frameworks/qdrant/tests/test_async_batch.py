"""Offline contracts across Qdrant's legacy and current public client APIs."""

import asyncio
import inspect
from http.server import BaseHTTPRequestHandler, HTTPServer
from threading import Thread

import pytest
import qdrant_client
from opentelemetry import context, trace
from opentelemetry.context import _SUPPRESS_INSTRUMENTATION_KEY
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
from opentelemetry.proto.collector.trace.v1.trace_service_pb2 import ExportTraceServiceRequest
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.trace import SpanKind, StatusCode
from qdrant_client import QdrantClient, models
from qdrant_client.http import models as rest_models

from traceai_qdrant import QdrantInstrumentor


# AsyncQdrantClient did not exist at the declared 1.6.0 dependency floor.
CLIENT_TYPES = [QdrantClient]
if hasattr(qdrant_client, "AsyncQdrantClient"):
    CLIENT_TYPES.append(qdrant_client.AsyncQdrantClient)


async def call(method, *args, **kwargs):
    result = method(*args, **kwargs)
    return await result if inspect.isawaitable(result) else result


async def seed(client):
    await call(client.create_collection, "documents", vectors_config=models.VectorParams(
        size=3, distance=models.Distance.COSINE))
    await call(client.upsert, "documents", points=[
        models.PointStruct(id=1, vector=[1.0, 0.0, 0.0], payload={"group": "a", "text": "PRIVATE_DOCUMENT"}),
        models.PointStruct(id=2, vector=[0.8, 0.2, 0.0], payload={"group": "b", "text": "PRIVATE_DOCUMENT"}),
    ])


def query(client):
    if hasattr(client, "query_points"):
        return client.query_points, {"query": [1.0, 0.0, 0.0]}
    return client.search, {"query_vector": [1.0, 0.0, 0.0]}


def batch(client):
    if hasattr(client, "query_batch_points"):
        return client.query_batch_points, [models.QueryRequest(query=[1.0, 0.0, 0.0], limit=1) for _ in range(2)]
    return client.search_batch, [models.SearchRequest(vector=[1.0, 0.0, 0.0], limit=1) for _ in range(2)]


def groups(client):
    if hasattr(client, "query_points_groups"):
        return client.query_points_groups, {"query": [1.0, 0.0, 0.0]}
    return client.search_groups, {"query_vector": [1.0, 0.0, 0.0]}


@pytest.mark.asyncio
@pytest.mark.parametrize("client_type", CLIENT_TYPES)
async def test_real_query_batch_groups_and_crud(client_type, instrument_qdrant, in_memory_span_exporter):
    client = client_type(":memory:")
    try:
        await seed(client)
        (upsert,) = in_memory_span_exporter.get_finished_spans()
        assert upsert.attributes["db.vector.upsert.count"] == 2
        in_memory_span_exporter.clear()
        method, params = query(client)
        response = await call(method, "documents", **params, limit=1, score_threshold=0.0)
        points = response.points if hasattr(response, "points") else response
        assert points[0].id == 1
        assert points[0].payload["text"] == "PRIVATE_DOCUMENT"
        (span,) = in_memory_span_exporter.get_finished_spans()
        assert span.name == "qdrant query"
        assert span.attributes["db.vector.results.count"] == 1
        assert span.attributes["db.vector.query.score_threshold"] == 0.0
        assert span.kind == SpanKind.CLIENT
        in_memory_span_exporter.clear()

        method, requests = batch(client)
        responses = await call(method, "documents", requests)  # Positional arguments must work too.
        assert len(responses) == 2
        assert all((r.points if hasattr(r, "points") else r)[0].id == 1 for r in responses)
        (span,) = in_memory_span_exporter.get_finished_spans()
        assert span.name == "qdrant query"
        assert span.attributes["db.vector.query.batch_size"] == 2
        assert span.attributes["db.vector.results.count"] == 2  # Hits, not number of requests.
        in_memory_span_exporter.clear()

        method, params = groups(client)
        response = await call(method, "documents", group_by="group", **params, limit=2, group_size=1)
        assert len(response.groups) == 2
        (span,) = in_memory_span_exporter.get_finished_spans()
        assert span.attributes["db.vector.results.groups_count"] == 2
        assert span.attributes["db.vector.results.count"] == 2
        assert span.attributes["db.vector.query.group_size"] == 1
        assert span.attributes["db.vector.query.top_k"] == 2
        in_memory_span_exporter.clear()

        assert (await call(client.count, "documents")).count == 2
        assert (await call(client.retrieve, "documents", [1]))[0].id == 1
        records, offset = await call(client.scroll, "documents", None, 1)
        assert records[0].id == 1 and offset == 2
        await call(client.delete, "documents", models.PointIdsList(points=[1]))
        spans = in_memory_span_exporter.get_finished_spans()
        assert [s.name for s in spans] == ["qdrant count", "qdrant retrieve", "qdrant scroll", "qdrant delete"]
        assert spans[1].attributes["db.vector.retrieve.ids_count"] == 1
        assert spans[2].attributes["db.vector.scroll.limit"] == 1
        assert spans[3].attributes["db.vector.delete.has_selector"] is True
        assert all(s.status.status_code == StatusCode.OK for s in spans)
        assert all(s.attributes["db.namespace"] == "documents" for s in spans)
    finally:
        await call(client.close)


@pytest.mark.asyncio
@pytest.mark.parametrize("client_type", CLIENT_TYPES)
async def test_empty_results_and_batch_upsert(client_type, instrument_qdrant, in_memory_span_exporter):
    client = client_type(":memory:")
    try:
        await seed(client)
        in_memory_span_exporter.clear()
        await call(client.upsert, "documents", models.Batch(ids=[3], vectors=[[0.0, 0.0, 1.0]]))
        (span,) = in_memory_span_exporter.get_finished_spans()
        assert span.attributes["db.vector.upsert.count"] == 1
        in_memory_span_exporter.clear()
        method, params = query(client)
        response = await call(method, "documents", **params, query_filter=models.Filter(
            must=[models.HasIdCondition(has_id=[999])]))
        assert not (response.points if hasattr(response, "points") else response)
        (span,) = in_memory_span_exporter.get_finished_spans()
        assert span.attributes["db.vector.results.count"] == 0
        in_memory_span_exporter.clear()
        method, _ = batch(client)
        assert await call(method, "documents", requests=[]) == []
        (span,) = in_memory_span_exporter.get_finished_spans()
        assert span.attributes["db.vector.results.count"] == 0
    finally:
        await call(client.close)


@pytest.mark.asyncio
@pytest.mark.parametrize("client_type", CLIENT_TYPES)
async def test_batch_and_group_counts_are_hits_not_containers(client_type, instrument_qdrant, in_memory_span_exporter):
    client = client_type(":memory:")
    try:
        await seed(client)
        in_memory_span_exporter.clear()
        method, requests = batch(client)
        # Two responses containing one and two hits must report three results.
        requests[1].limit = 2
        await call(method, "documents", requests)
        (span,) = in_memory_span_exporter.get_finished_spans()
        assert span.attributes["db.vector.query.batch_size"] == 2
        assert span.attributes["db.vector.results.count"] == 3
        assert "db.vector.query.top_k" not in span.attributes
        in_memory_span_exporter.clear()
        await call(client.set_payload, "documents", payload={"group": "a"}, points=[2])
        method, params = groups(client)
        result = await call(method, "documents", **params, group_by="group", group_size=2, limit=1)
        assert len(result.groups) == 1 and len(result.groups[0].hits) == 2
        (span,) = in_memory_span_exporter.get_finished_spans()
        assert span.attributes["db.vector.results.groups_count"] == 1
        assert span.attributes["db.vector.results.count"] == 2
    finally:
        await call(client.close)


@pytest.mark.asyncio
@pytest.mark.parametrize("client_type", CLIENT_TYPES)
async def test_failure_and_suppression(client_type, instrument_qdrant, in_memory_span_exporter):
    client = client_type(":memory:")
    try:
        method, params = query(client)
        with pytest.raises(ValueError, match="not found"):
            await call(method, "missing", **params)
        (span,) = in_memory_span_exporter.get_finished_spans()
        assert span.status.status_code == StatusCode.ERROR
        assert len(span.events) == 1
        assert span.events[0].name == "exception"
        in_memory_span_exporter.clear()
        token = context.attach(context.set_value(_SUPPRESS_INSTRUMENTATION_KEY, True))
        try:
            await seed(client)
            await call(method, "documents", **params)
        finally:
            context.detach(token)
        assert in_memory_span_exporter.get_finished_spans() == ()
    finally:
        await call(client.close)


@pytest.mark.asyncio
@pytest.mark.parametrize("client_type", CLIENT_TYPES)
async def test_legacy_recommendations(client_type, instrument_qdrant, in_memory_span_exporter):
    client = client_type(":memory:")
    try:
        # Modern SDKs use RecommendQuery with query_points instead of these APIs.
        if not hasattr(client, "recommend"):
            assert hasattr(client, "query_points")
            await seed(client)
            in_memory_span_exporter.clear()
            result = await call(client.query_points, "documents", query=models.RecommendQuery(
                recommend=models.RecommendInput(positive=[1])), limit=1)
            assert result.points[0].id == 2
            (span,) = in_memory_span_exporter.get_finished_spans()
            assert span.attributes["db.vector.results.count"] == 1
            return
        await seed(client)
        in_memory_span_exporter.clear()
        assert (await call(client.recommend, "documents", [1], None, limit=1))[0].id == 2
        assert (await call(client.recommend_batch, "documents", [models.RecommendRequest(positive=[1], limit=1)]))[0][0].id == 2
        assert (await call(client.recommend_groups, "documents", group_by="group", positive=[1], limit=1)).groups[0].hits[0].id == 2
        spans = in_memory_span_exporter.get_finished_spans()
        assert len(spans) == 3
        assert all(s.name == "qdrant recommend" for s in spans)
        assert all(s.attributes["db.vector.results.count"] == 1 for s in spans)
        assert spans[0].attributes["db.vector.recommend.positive_count"] == 1
        assert spans[0].attributes["db.vector.recommend.negative_count"] == 0
    finally:
        await call(client.close)


@pytest.mark.parametrize("client_type", CLIENT_TYPES)
def test_uninstrument_restores_all_available_methods(client_type, tracer_provider):
    names = ("query", "query_batch", "query_points", "query_batch_points", "query_points_groups",
             "search", "search_batch", "search_groups", "recommend", "recommend_batch", "recommend_groups",
             "upsert", "delete", "retrieve", "scroll", "count")
    originals = {name: getattr(client_type, name, None) for name in names}
    instrumentor = QdrantInstrumentor()
    instrumentor.instrument(tracer_provider=tracer_provider)
    try:
        for name, original in originals.items():
            if original is not None:
                assert getattr(client_type, name) is not original
            else:
                assert not hasattr(client_type, name)
    finally:
        instrumentor.uninstrument()
    for name, original in originals.items():
        assert getattr(client_type, name, None) is original


@pytest.mark.asyncio
@pytest.mark.parametrize("client_type", [c for c in CLIENT_TYPES if c is not QdrantClient])
async def test_async_lifetime_cancellation_and_concurrent_parents(client_type, instrument_qdrant, in_memory_span_exporter, tracer_provider, monkeypatch):
    client = client_type(":memory:")
    entered = asyncio.Event()
    release = asyncio.Event()
    original_error = asyncio.CancelledError("cancelled offline query")

    async def blocked(**kwargs):
        assert trace.get_current_span().is_recording()
        entered.set()
        await release.wait()
        raise original_error

    monkeypatch.setattr(client._client, "query_points", blocked)
    try:
        task = asyncio.create_task(client.query_points("documents", query=[1.0, 0.0, 0.0]))
        await asyncio.wait_for(entered.wait(), timeout=5)
        assert in_memory_span_exporter.get_finished_spans() == ()
        release.set()
        with pytest.raises(asyncio.CancelledError) as exc:
            await task
        assert exc.value is original_error
        (span,) = in_memory_span_exporter.get_finished_spans()
        assert span.name == "qdrant query"
        assert span.end_time is not None
        assert not trace.get_current_span().is_recording()
        in_memory_span_exporter.clear()

        entered.clear()
        release.clear()
        task = asyncio.create_task(client.query_points("documents", query=[1.0, 0.0, 0.0]))
        await asyncio.wait_for(entered.wait(), timeout=5)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        (cancelled_span,) = in_memory_span_exporter.get_finished_spans()
        assert cancelled_span.end_time is not None
        assert not trace.get_current_span().is_recording()
        in_memory_span_exporter.clear()

        async def response(**kwargs):
            await asyncio.sleep(0)
            return rest_models.QueryResponse(points=[])
        monkeypatch.setattr(client._client, "query_points", response)
        tracer = tracer_provider.get_tracer("concurrency")
        async def worker(name):
            with tracer.start_as_current_span(name):
                await client.query_points("documents", query=[1.0, 0.0, 0.0])
        await asyncio.gather(worker("parent-a"), worker("parent-b"))
        spans = in_memory_span_exporter.get_finished_spans()
        parents = {s.context.span_id: s for s in spans if s.name.startswith("parent-")}
        children = [s for s in spans if s.name == "qdrant query"]
        assert len(children) == 2
        assert {s.parent.span_id for s in children} == set(parents)
        assert all(s.context.trace_id == parents[s.parent.span_id].context.trace_id for s in children)
    finally:
        await client.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("client_type", CLIENT_TYPES)
async def test_otlp_wire_metadata_without_document_vector_or_filter_content(client_type):
    requests = []
    class Collector(BaseHTTPRequestHandler):
        def do_POST(self):
            assert self.path == "/v1/traces"
            requests.append(self.rfile.read(int(self.headers["Content-Length"])))
            self.send_response(200)
            self.end_headers()
        def log_message(self, *args):
            pass
    server = HTTPServer(("127.0.0.1", 0), Collector)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    provider = TracerProvider()
    exporter = OTLPSpanExporter(endpoint=f"http://127.0.0.1:{server.server_port}/v1/traces", timeout=2)
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    instrumentor = QdrantInstrumentor()
    instrumentor.instrument(tracer_provider=provider)
    client = client_type(":memory:")
    try:
        await seed(client)
        requests.clear()
        method, params = query(client)
        await call(method, "documents", **params, query_filter=models.Filter(must=[
            models.FieldCondition(key="text", match=models.MatchValue(value="PRIVATE_DOCUMENT"))
        ]), with_payload=models.PayloadSelectorInclude(include=["text"]), with_vectors=True, limit=1)
        method, batch_requests = batch(client)
        await call(method, "documents", batch_requests)
        method, params = groups(client)
        await call(method, "documents", **params, group_by="group", limit=2)
        spans = [span for body in requests
                 for resource in ExportTraceServiceRequest.FromString(body).resource_spans
                 for scope in resource.scope_spans for span in scope.spans]
        assert len(spans) == 3
        for span in spans:
            assert span.name == "qdrant query"
            assert span.kind == 3  # CLIENT on the OTLP wire.
            assert span.start_time_unix_nano <= span.end_time_unix_nano
            attrs = {a.key: a.value for a in span.attributes}
            assert attrs["db.system"].string_value == "qdrant"
            assert attrs["db.namespace"].string_value == "documents"
            assert attrs["db.vector.results.count"].int_value > 0
            assert "db.vector.query.filter" not in attrs
        assert all(b"PRIVATE_DOCUMENT" not in body for body in requests)
        assert all(b"[1.0, 0.0, 0.0]" not in body for body in requests)
    finally:
        await call(client.close)
        instrumentor.uninstrument()
        provider.shutdown()
        server.shutdown()
        thread.join(timeout=5)
        server.server_close()
