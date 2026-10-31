"""Exercise real sync/async Chroma methods; only their transport is synthetic."""

import asyncio
import inspect
import json
from http.server import BaseHTTPRequestHandler, HTTPServer
from threading import Thread

import numpy as np
import pytest
from opentelemetry import context, trace
from opentelemetry.context import _SUPPRESS_INSTRUMENTATION_KEY
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.trace import SpanKind, StatusCode

from traceai_chromadb import ChromaDBInstrumentor


def call(collection, name, *args, **kwargs):
    result = getattr(collection, name)(*args, **kwargs)
    return asyncio.run(result) if inspect.isawaitable(result) else result


def positional(method, **values):
    """Use each installed Chroma version's actual positional argument order."""
    params = inspect.signature(method).parameters
    last = max(list(params).index(name) for name in values)
    return [
        values.get(name, param.default)
        for name, param in list(params.items())[: last + 1]
    ]


def span_for(exporter, operation):
    spans = exporter.get_finished_spans()
    assert len(spans) == 1
    span = spans[0]
    assert span.name == f"chroma {operation}"
    assert span.kind == SpanKind.CLIENT
    assert span.attributes["db.system"] == "chroma"
    assert span.attributes["db.namespace"] == "test-collection"
    assert span.attributes["db.operation.name"] == operation
    assert span.status.status_code == StatusCode.OK
    return span


@pytest.mark.parametrize("operation", ["add", "upsert", "update"])
@pytest.mark.parametrize("use_positional", [False, True])
def test_writes(
    collection, instrument_chromadb, in_memory_span_exporter, operation, use_positional
):
    values = dict(
        ids=["a", "b"],
        embeddings=[[1.0, 0.0, 0.0], [0.0, 1.0, 0.0]],
        metadatas=[{"source": "test"}, {"source": "test"}],
        documents=["one", "two"],
    )
    if use_positional:
        result = call(
            collection, operation, *positional(getattr(collection, operation), **values)
        )
    else:
        result = call(collection, operation, **values)
    assert result is None
    assert len(collection._client.calls) == 1
    span = span_for(in_memory_span_exporter, operation)
    if operation == "update":
        assert span.attributes["db.vector.update.count"] == 2
        assert span.attributes["db.vector.update.has_embeddings"] is True
    else:
        assert span.attributes["db.vector.upsert.count"] == 2
        assert span.attributes["db.vector.upsert.dimensions"] == 3
        assert span.attributes["db.vector.documents.count"] == 2


@pytest.mark.parametrize("operation", ["add", "upsert", "update", "query"])
@pytest.mark.parametrize("single", [False, True])
@pytest.mark.parametrize("use_positional", [False, True])
def test_numpy_inputs(
    collection,
    instrument_chromadb,
    in_memory_span_exporter,
    operation,
    single,
    use_positional,
):
    # Chroma 0.4.0 itself rejects ndarray inputs. Its list path is covered above.
    if "query_images" not in inspect.signature(collection.query).parameters:
        pytest.skip("NumPy input support is absent in Chroma 0.4.0")
    embeddings = np.array(
        [1.0, 0.0, 0.0] if single else [[1.0, 0.0, 0.0], [0.0, 1.0, 0.0]],
        dtype=np.float32,
    )
    original = embeddings.copy()
    count = 1 if single else 2
    if operation == "query":
        values = dict(query_embeddings=embeddings, n_results=1)
    else:
        values = dict(ids="a" if single else ["a", "b"], embeddings=embeddings)
    if use_positional:
        result = call(
            collection, operation, *positional(getattr(collection, operation), **values)
        )
    else:
        result = call(collection, operation, **values)
    if operation == "query":
        assert result["ids"] == [["a", "b"]]
    np.testing.assert_array_equal(embeddings, original)
    span = span_for(in_memory_span_exporter, operation)
    if operation == "query":
        assert span.attributes["db.vector.query.embedding_count"] == count
        assert span.attributes["db.vector.query.top_k"] == 1
    elif operation == "update":
        assert span.attributes["db.vector.update.count"] == count
    else:
        assert span.attributes["db.vector.upsert.count"] == count
        assert span.attributes["db.vector.upsert.dimensions"] == 3


def test_positional_query_and_parent(
    collection, instrument_chromadb, tracer_provider, in_memory_span_exporter
):
    values = dict(
        query_embeddings=[[1.0, 0.0, 0.0]],
        n_results=1,
        where={"source": "test"},
        where_document={"$contains": "test"},
        include=["distances"],
    )
    with tracer_provider.get_tracer("test").start_as_current_span("parent") as parent:
        result = call(collection, "query", *positional(collection.query, **values))
        span = span_for(in_memory_span_exporter, "query")
        assert span.parent.span_id == parent.get_span_context().span_id
        assert trace.get_current_span() is parent
    assert result["ids"] == [["a", "b"]]
    assert span.attributes["db.vector.query.top_k"] == 1
    assert json.loads(span.attributes["db.vector.query.filter"]) == values["where"]
    assert span.attributes["db.vector.results.count"] == 2
    assert json.loads(span.attributes["db.vector.results.ids"]) == ["a", "b"]
    assert json.loads(span.attributes["db.vector.results.scores"]) == [0.0, 0.5]


@pytest.mark.parametrize("operation", ["get", "delete", "count", "peek"])
def test_other_operations(
    collection, instrument_chromadb, in_memory_span_exporter, operation
):
    values = {
        "get": dict(ids="a", limit=1, offset=0),
        "delete": dict(ids=["a"]),
        "count": {},
        "peek": dict(limit=1),
    }[operation]
    args = positional(getattr(collection, operation), **values) if values else []
    result = call(collection, operation, *args)
    span = span_for(in_memory_span_exporter, operation)
    if operation == "get":
        assert span.attributes["db.vector.get.ids_count"] == 1
        assert span.attributes["db.vector.get.offset"] == 0
        assert span.attributes["db.vector.get.limit"] == 1
        assert span.attributes["db.vector.results.count"] == 2
    elif operation == "delete":
        assert span.attributes["db.vector.delete.count"] == 1
    elif operation == "count":
        assert result == span.attributes["db.vector.collection.count"] == 2
    else:
        assert span.attributes["db.vector.peek.limit"] == 1
        assert span.attributes["db.vector.results.count"] == 2


@pytest.mark.parametrize("batch", [False, True])
def test_search(collection, instrument_chromadb, in_memory_span_exporter, batch):
    if not hasattr(collection, "search"):
        pytest.skip("Search API absent from installed Chroma")
    from chromadb import Search

    search = Search().limit(1)
    result = call(collection, "search", [search, search] if batch else search)
    assert result["ids"] == [["a", "b"]]
    span = span_for(in_memory_span_exporter, "search")
    assert span.attributes["db.vector.search.count"] == (2 if batch else 1)
    assert span.attributes["db.vector.results.count"] == 2
    assert json.loads(span.attributes["db.vector.results.scores"]) == [0.0, 0.5]


def test_error_propagation(collection, instrument_chromadb, in_memory_span_exporter):
    error = RuntimeError("synthetic transport failure")
    collection._client.error = error
    with pytest.raises(RuntimeError) as caught:
        call(collection, "count")
    assert caught.value is error
    spans = in_memory_span_exporter.get_finished_spans()
    assert len(spans) == 1
    assert spans[0].status.status_code == StatusCode.ERROR
    assert len(spans[0].events) == 1
    assert spans[0].events[0].attributes["exception.message"] == str(error)
    assert not trace.get_current_span().is_recording()


def test_validation_error_preserved(
    collection, instrument_chromadb, in_memory_span_exporter
):
    with pytest.raises((TypeError, ValueError)):
        call(
            collection,
            "add",
            ids=["a"],
            embeddings=[[1.0, 0.0]],
            unexpected_argument=True,
        )
    spans = in_memory_span_exporter.get_finished_spans()
    assert len(spans) == 1
    assert spans[0].status.status_code == StatusCode.ERROR
    assert collection._client.calls == []


def test_suppression(collection, instrument_chromadb, in_memory_span_exporter):
    token = context.attach(context.set_value(_SUPPRESS_INSTRUMENTATION_KEY, True))
    try:
        assert call(collection, "count") == 2
    finally:
        context.detach(token)
    assert in_memory_span_exporter.get_finished_spans() == ()
    assert call(collection, "count") == 2
    span_for(in_memory_span_exporter, "count")


def test_uninstrument_and_reinstrument(
    collection, tracer_provider, in_memory_span_exporter
):
    original = type(collection).count
    instrumentor = ChromaDBInstrumentor()
    try:
        for _ in range(2):
            instrumentor.instrument(tracer_provider=tracer_provider)
            instrumentor.instrument(tracer_provider=tracer_provider)
            assert call(collection, "count") == 2
            span_for(in_memory_span_exporter, "count")
            instrumentor.uninstrument()
            assert type(collection).count is original
            in_memory_span_exporter.clear()
            assert call(collection, "count") == 2
            assert in_memory_span_exporter.get_finished_spans() == ()
    finally:
        if instrumentor.is_instrumented_by_opentelemetry:
            instrumentor.uninstrument()


def test_async_span_lifetime_and_cancellation(
    collection, instrument_chromadb, in_memory_span_exporter, monkeypatch
):
    if not inspect.iscoroutinefunction(collection.count):
        pytest.skip("Async-only lifecycle check")

    async def run():
        started = asyncio.Event()
        release = asyncio.Event()

        async def count(**kwargs):
            assert trace.get_current_span().is_recording()
            started.set()
            await release.wait()
            return 2

        monkeypatch.setattr(collection._client, "_count", count)
        task = asyncio.create_task(collection.count())
        await started.wait()
        assert in_memory_span_exporter.get_finished_spans() == ()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert len(in_memory_span_exporter.get_finished_spans()) == 1
        in_memory_span_exporter.clear()
        release.set()
        assert await collection.count() == 2
        span_for(in_memory_span_exporter, "count")
        assert not trace.get_current_span().is_recording()

    asyncio.run(run())


def test_nested_collection_call_is_not_duplicated(
    collection, tracer_provider, in_memory_span_exporter, monkeypatch
):
    cls = type(collection)
    if inspect.iscoroutinefunction(cls.peek):

        async def peek(self, limit=10):
            return await self.get(limit=limit)
    else:

        def peek(self, limit=10):
            return self.get(limit=limit)

    monkeypatch.setattr(cls, "peek", peek)
    instrumentor = ChromaDBInstrumentor()
    instrumentor.instrument(tracer_provider=tracer_provider)
    try:
        call(collection, "peek", 1)
        span_for(in_memory_span_exporter, "peek")
    finally:
        instrumentor.uninstrument()


def test_attribute_failure_does_not_change_result(
    collection, instrument_chromadb, in_memory_span_exporter, monkeypatch
):
    from traceai_chromadb._wrappers import CountWrapper

    def fail(*args):
        raise ValueError("synthetic attribute failure")

    monkeypatch.setattr(CountWrapper, "_request_attributes", fail)
    monkeypatch.setattr(CountWrapper, "_result_attributes", fail)
    assert call(collection, "count") == 2
    span_for(in_memory_span_exporter, "count")


def test_empty_query_result(collection, instrument_chromadb, in_memory_span_exporter):
    collection._client.response = {"ids": [[]], "distances": [[]], "uris": None}
    assert call(collection, "query", [[1.0, 0.0, 0.0]], include=["distances"])[
        "ids"
    ] == [[]]
    span = span_for(in_memory_span_exporter, "query")
    assert span.attributes["db.vector.results.count"] == 0
    assert "db.vector.results.ids" not in span.attributes


def test_install_failure_restores_all_methods(tracer_provider, monkeypatch):
    import traceai_chromadb
    from chromadb.api.models.Collection import Collection

    originals = {
        name: getattr(Collection, name)
        for name in traceai_chromadb._WRAPPERS
        if hasattr(Collection, name)
    }
    wrap = traceai_chromadb.wrap_function_wrapper

    def fail(target, name, callback):
        if name == "get":
            raise RuntimeError("synthetic patch failure")
        return wrap(target, name, callback)

    monkeypatch.setattr(traceai_chromadb, "wrap_function_wrapper", fail)
    with pytest.raises(RuntimeError, match="synthetic patch failure"):
        ChromaDBInstrumentor().instrument(tracer_provider=tracer_provider)
    assert not ChromaDBInstrumentor().is_instrumented_by_opentelemetry
    for name, original in originals.items():
        assert getattr(Collection, name) is original


def test_async_concurrency(
    collection, instrument_chromadb, tracer_provider, in_memory_span_exporter
):
    if not inspect.iscoroutinefunction(collection.count):
        pytest.skip("Async-only concurrency check")

    async def operation(name):
        with tracer_provider.get_tracer("test").start_as_current_span(name) as parent:
            assert await collection.count() == 2
            assert trace.get_current_span() is parent
            return parent.get_span_context().span_id

    async def run():
        return await asyncio.gather(operation("first"), operation("second"))

    parents = asyncio.run(run())
    spans = in_memory_span_exporter.get_finished_spans()
    counts = [span for span in spans if span.name == "chroma count"]
    assert len(spans) == 4
    assert len(counts) == 2
    assert {span.parent.span_id for span in counts} == set(parents)


def test_otlp_http_wire(collection, instrument_chromadb, tracer_provider):
    from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
    from opentelemetry.proto.collector.trace.v1.trace_service_pb2 import (
        ExportTraceServiceRequest,
    )

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
    exporter = OTLPSpanExporter(
        endpoint=f"http://127.0.0.1:{server.server_port}/v1/traces", headers={}
    )
    tracer_provider.add_span_processor(SimpleSpanProcessor(exporter))
    try:
        call(
            collection,
            "query",
            *positional(
                collection.query, query_embeddings=[[1.0, 0.0, 0.0]], n_results=1
            ),
        )
        assert tracer_provider.force_flush()
        assert len(requests) == 1
        request = ExportTraceServiceRequest.FromString(requests[0])
        scopes = [
            scope
            for resource in request.resource_spans
            for scope in resource.scope_spans
        ]
        assert len(scopes) == 1
        assert scopes[0].scope.name == "traceai_chromadb"
        assert len(scopes[0].spans) == 1
        span = scopes[0].spans[0]
        assert span.name == "chroma query"
        assert span.kind == span.SPAN_KIND_CLIENT
        attrs = {entry.key: entry.value for entry in span.attributes}
        assert attrs["db.vector.query.top_k"].int_value == 1
        assert attrs["db.vector.results.count"].int_value == 2
        assert attrs["db.namespace"].string_value == "test-collection"
        assert span.end_time_unix_nano >= span.start_time_unix_nano
        assert b"private-document" not in requests[0]
        assert not any("embeddings" in key for key in attrs)
    finally:
        exporter.shutdown()
        server.shutdown()
        thread.join(timeout=5)
        server.server_close()
