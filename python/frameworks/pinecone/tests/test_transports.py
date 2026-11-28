"""Real SDK entry points with loopback REST and synthetic gRPC transports."""

import asyncio
import importlib
import inspect
import os
from types import SimpleNamespace
from unittest.mock import MagicMock

import pinecone
import pytest
from opentelemetry import context
from opentelemetry.context import _SUPPRESS_INSTRUMENTATION_KEY
from opentelemetry.exporter.otlp.proto.common.trace_encoder import encode_spans
from opentelemetry.proto.collector.trace.v1.trace_service_pb2 import ExportTraceServiceRequest
from opentelemetry.trace import StatusCode

from traceai_pinecone import PineconeInstrumentor
from .test_instrumentor import Index, endpoint  # noqa: F401


OPERATIONS = {
    "query": {"vector": [0.1, 0.2, 0.3], "top_k": 2, "namespace": "test-ns"},
    "upsert": {"vectors": [{"id": "vec1", "values": [0.1, 0.2, 0.3]}], "namespace": "test-ns"},
    "delete": {"ids": ["vec1"], "namespace": "test-ns"},
    "fetch": {"ids": ["vec1"], "namespace": "test-ns"},
    "update": {"id": "vec1", "values": [0.1, 0.2, 0.3], "namespace": "test-ns"},
    "describe_index_stats": {},
}


def available_classes():
    classes = {"rest": Index}
    if hasattr(pinecone, "AsyncIndex"):
        classes["async"] = pinecone.AsyncIndex
    else:
        try:
            from pinecone.db_data import IndexAsyncio
            # Some SDK releases defer the optional aiohttp import to construction.
            import aiohttp  # noqa: F401
        except ImportError:
            pass
        else:
            classes["async"] = IndexAsyncio
    try:
        grpc = importlib.import_module("pinecone.grpc")
    except ImportError:
        pass
    else:
        classes["grpc"] = getattr(grpc, "GrpcIndex", None) or grpc.GRPCIndex
    return classes


CLASSES = available_classes()


def test_required_transports_are_installed():
    if os.environ.get("PINECONE_REQUIRE_TRANSPORTS") == "1":
        # SDK 3's grpc extra pins protobuf 3, incompatible with OTel >=1.29.
        # Keep its REST floor; test optional transports on a compatible SDK.
        if int(pinecone.__version__.split(".")[0]) >= 6:
            assert {"async", "grpc"} <= CLASSES.keys()


@pytest.mark.parametrize("kind", CLASSES)
def test_all_methods_wrap_once_and_restore(kind, tracer_provider):
    cls = CLASSES[kind]
    originals = {name: getattr(cls, name) for name in OPERATIONS}
    instrumentor = PineconeInstrumentor()
    for _ in range(2):
        instrumentor.instrument(tracer_provider=tracer_provider)
        try:
            for name, original in originals.items():
                assert getattr(cls, name) is not original
                assert getattr(cls, name).__wrapped__ is original
            instrumentor.instrument(tracer_provider=tracer_provider)
            for name, original in originals.items():
                assert getattr(cls, name).__wrapped__ is original
        finally:
            instrumentor.uninstrument()
        assert all(getattr(cls, name) is original for name, original in originals.items())


def assert_operation(span, operation):
    assert span.name == f"pinecone {operation}"
    assert span.attributes["db.system"] == "pinecone"
    assert span.attributes["db.operation.name"] == operation
    assert span.status.status_code == StatusCode.OK
    if operation != "describe_index_stats":
        assert span.attributes["db.vector.namespace"] == "test-ns"
    if operation in ("query", "fetch"):
        assert span.attributes["db.vector.results.count"] == 1
    if operation == "upsert":
        assert span.attributes["db.vector.upsert.count"] == 1
        assert span.attributes["db.vector.upsert.dimensions"] == 3
        assert span.attributes["db.vector.upserted_count"] == 2
    if operation == "describe_index_stats":
        assert span.attributes["db.vector.stats.total_count"] == 2
        assert span.attributes["db.vector.index.dimensions"] == 3


@pytest.mark.parametrize("operation", OPERATIONS)
@pytest.mark.parametrize("kind", [k for k in CLASSES if k != "grpc"])
@pytest.mark.asyncio
async def test_rest_operations(kind, operation, endpoint, instrument_pinecone, in_memory_span_exporter):
    index = CLASSES[kind](api_key="test-not-a-secret", host=endpoint[0])
    try:
        result = getattr(index, operation)(**OPERATIONS[operation])
        if kind == "async":
            assert inspect.isawaitable(result)
            assert not in_memory_span_exporter.get_finished_spans()
            result = await result
        if operation == "query":
            assert result.matches[0].id == "vec1"
        span, = in_memory_span_exporter.get_finished_spans()
        assert_operation(span, operation)
        assert span.attributes["db.vector.index.host"] == endpoint[0]
    finally:
        if hasattr(index, "close"):
            result = index.close()
            if inspect.isawaitable(result):
                await result


@pytest.fixture
def grpc_index(monkeypatch):
    """Only replace the transport: SDK validation, requests and decoding stay real."""
    cls = CLASSES["grpc"]
    transport = MagicMock()
    if cls.__name__ == "GrpcIndex":
        transport.query.return_value = {"matches": [{"id": "vec1", "score": 0.95}], "namespace": "test-ns"}
        transport.fetch.return_value = {"vectors": {"vec1": {"values": [0.1, 0.2, 0.3]}}, "namespace": "test-ns"}
        transport.upsert.return_value = {"upserted_count": 2}
        transport.describe_index_stats.return_value = {"total_vector_count": 2, "dimension": 3, "namespaces": {}}
        native = importlib.import_module("pinecone._grpc")
        monkeypatch.setattr(native, "GrpcChannel", lambda *args, **kwargs: transport)
        index = cls(api_key="test-not-a-secret", host="http://127.0.0.1:9", secure=False)
    else:
        try:
            pb = importlib.import_module("pinecone.core.grpc.protos.db_data_2025_04_pb2")
        except ImportError:
            pb = importlib.import_module("pinecone.core.grpc.protos.vector_service_pb2")
        transport.Query.return_value = pb.QueryResponse(matches=[pb.ScoredVector(id="vec1", score=0.95)], namespace="test-ns")
        transport.Fetch.return_value = pb.FetchResponse(vectors={"vec1": pb.Vector(id="vec1", values=[0.1, 0.2, 0.3])}, namespace="test-ns")
        transport.Upsert.return_value = pb.UpsertResponse(upserted_count=2)
        transport.Delete.return_value = pb.DeleteResponse()
        transport.Update.return_value = pb.UpdateResponse()
        transport.DescribeIndexStats.return_value = pb.DescribeIndexStatsResponse(total_vector_count=2, dimension=3)
        index = cls(index_name="test-index", config=pinecone.Config(api_key="test-not-a-secret", host="http://127.0.0.1:9"), channel=MagicMock())
        index.stub = transport
    try:
        yield index, transport
    finally:
        index.close()


@pytest.mark.parametrize("kind", [k for k in CLASSES if k == "grpc"])
@pytest.mark.parametrize("operation", OPERATIONS)
def test_grpc_operations(kind, operation, grpc_index, instrument_pinecone, in_memory_span_exporter):
    index, transport = grpc_index
    result = getattr(index, operation)(**OPERATIONS[operation])
    assert transport.mock_calls
    if operation == "query":
        assert result.matches[0].id == "vec1"
    span, = in_memory_span_exporter.get_finished_spans()
    assert_operation(span, operation)


@pytest.mark.parametrize("kind", [k for k in CLASSES if k == "grpc"])
def test_grpc_error_and_parent_on_otlp_wire(kind, grpc_index, instrument_pinecone, tracer_provider, in_memory_span_exporter):
    index, transport = grpc_index
    error = ValueError("synthetic transport failure")
    method = transport.query if type(index).__name__ == "GrpcIndex" else transport.Query
    method.side_effect = error
    with tracer_provider.get_tracer("test").start_as_current_span("parent") as parent:
        with pytest.raises(ValueError) as caught:
            index.query(**OPERATIONS["query"])
        assert caught.value is error
    spans = in_memory_span_exporter.get_finished_spans()
    child, _ = spans
    assert child.parent.span_id == parent.get_span_context().span_id
    assert child.status.status_code == StatusCode.ERROR
    assert len(child.events) == 1
    wire = ExportTraceServiceRequest.FromString(encode_spans(spans).SerializeToString())
    exported = [s for r in wire.resource_spans for scope in r.scope_spans for s in scope.spans]
    assert [s.name for s in exported] == ["pinecone query", "parent"]
    assert exported[0].parent_span_id == exported[1].span_id
    assert exported[0].kind == 3  # CLIENT
    assert exported[0].status.code == 2  # ERROR


@pytest.mark.parametrize("kind", [k for k in CLASSES if k == "async"])
@pytest.mark.parametrize("outcome", ["success", "error", "cancel"])
@pytest.mark.asyncio
async def test_awaited_lifetime(kind, outcome, monkeypatch, tracer_provider, in_memory_span_exporter):
    cls = CLASSES[kind]
    entered, resume = asyncio.Event(), asyncio.Event()
    error = ValueError("synthetic async failure")
    response = SimpleNamespace(matches=[SimpleNamespace(id="vec1", score=0.95)])

    async def query(self, **kwargs):
        entered.set()
        await resume.wait()
        if outcome == "error":
            raise error
        return response

    # Exercise lifecycle independently of HTTP timing with a real SDK class.
    monkeypatch.setattr(cls, "query", query)
    instrumentor = PineconeInstrumentor()
    instrumentor.instrument(tracer_provider=tracer_provider)
    try:
        index = cls.__new__(cls)
        with tracer_provider.get_tracer("test").start_as_current_span("parent") as parent:
            task = asyncio.create_task(index.query(**OPERATIONS["query"]))
            await asyncio.wait_for(entered.wait(), timeout=2)
            assert not in_memory_span_exporter.get_finished_spans()
            if outcome == "cancel":
                task.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await task
            else:
                resume.set()
                if outcome == "error":
                    with pytest.raises(ValueError) as caught:
                        await task
                    assert caught.value is error
                else:
                    assert await task is response
        child, _ = in_memory_span_exporter.get_finished_spans()
        assert child.parent.span_id == parent.get_span_context().span_id
        assert child.status.status_code == (StatusCode.OK if outcome == "success" else StatusCode.ERROR)
        assert len(child.events) == (0 if outcome == "success" else 1)
        wire = ExportTraceServiceRequest.FromString(encode_spans([child]).SerializeToString())
        exported = wire.resource_spans[0].scope_spans[0].spans[0]
        assert exported.end_time_unix_nano >= exported.start_time_unix_nano
        assert exported.parent_span_id == parent.get_span_context().span_id.to_bytes(8, "big")
    finally:
        instrumentor.uninstrument()


@pytest.mark.parametrize("kind", [k for k in CLASSES if k != "async"])
def test_suppression_and_nested_calls(kind, monkeypatch, tracer_provider, in_memory_span_exporter):
    cls = CLASSES[kind]
    response = SimpleNamespace(matches=[])

    def query(self, **kwargs):
        if not kwargs.get("nested"):
            return self.query(nested=True)
        return response

    monkeypatch.setattr(cls, "query", query)
    instrumentor = PineconeInstrumentor()
    instrumentor.instrument(tracer_provider=tracer_provider)
    try:
        index = cls.__new__(cls)
        assert index.query() is response
        assert len(in_memory_span_exporter.get_finished_spans()) == 1
        token = context.attach(context.set_value(_SUPPRESS_INSTRUMENTATION_KEY, True))
        try:
            assert index.query() is response
        finally:
            context.detach(token)
        assert len(in_memory_span_exporter.get_finished_spans()) == 1
    finally:
        instrumentor.uninstrument()


def test_optional_imports_and_aliases(monkeypatch, tracer_provider):
    import traceai_pinecone as instrumentation

    def optional_import(name):
        if name == "pinecone":
            return SimpleNamespace(Index=Index)
        raise ImportError("optional SDK component not installed")

    original = Index.query
    monkeypatch.setattr(instrumentation, "import_module", optional_import)
    instrumentor = PineconeInstrumentor()
    instrumentor.instrument(tracer_provider=tracer_provider)
    try:
        assert Index.query.__wrapped__ is original
    finally:
        instrumentor.uninstrument()
    assert Index.query is original
    monkeypatch.setattr(instrumentation, "import_module", lambda name: SimpleNamespace(Index=Index, IndexAsyncio=Index, GrpcIndex=Index))
    assert list(instrumentation._index_classes()) == [Index]


def test_partial_instrumentation_rolls_back(monkeypatch, tracer_provider):
    import traceai_pinecone as instrumentation

    original = Index.query
    wrap = instrumentation.wrap_function_wrapper

    def failing_wrap(cls, name, wrapper):
        if name == "upsert":
            raise RuntimeError("synthetic wrapping failure")
        wrap(cls, name, wrapper)

    monkeypatch.setattr(instrumentation, "wrap_function_wrapper", failing_wrap)
    with pytest.raises(RuntimeError, match="synthetic wrapping failure"):
        PineconeInstrumentor().instrument(tracer_provider=tracer_provider)
    assert Index.query is original
    assert not PineconeInstrumentor().is_instrumented_by_opentelemetry


@pytest.mark.parametrize("positional", [False, True])
def test_legacy_future_is_unchanged(positional, monkeypatch, tracer_provider, in_memory_span_exporter):
    future = object()

    def upsert(self, vectors, async_req=False, namespace=None, **kwargs):
        assert vectors == [("vec1", [0.1, 0.2, 0.3])]
        assert async_req is True
        return future

    monkeypatch.setattr(Index, "upsert", upsert)
    instrumentor = PineconeInstrumentor()
    instrumentor.instrument(tracer_provider=tracer_provider)
    try:
        index = Index.__new__(Index)
        vectors = [("vec1", [0.1, 0.2, 0.3])]
        result = index.upsert(vectors, True) if positional else index.upsert(vectors=vectors, async_req=True)
        assert result is future
        assert not in_memory_span_exporter.get_finished_spans()
    finally:
        instrumentor.uninstrument()


def test_attribute_failures_preserve_sdk_result(monkeypatch, tracer_provider, in_memory_span_exporter, caplog):
    caplog.set_level("DEBUG", logger="traceai_pinecone._wrappers")
    class UninspectableVectors:
        def __len__(self):
            raise ValueError("instrumentation cannot count these")

    vectors = UninspectableVectors()
    result = object()

    def upsert(self, vectors):
        assert isinstance(vectors, UninspectableVectors)
        return result

    monkeypatch.setattr(Index, "upsert", upsert)
    instrumentor = PineconeInstrumentor()
    instrumentor.instrument(tracer_provider=tracer_provider)
    try:
        assert Index.__new__(Index).upsert(vectors) is result
        span, = in_memory_span_exporter.get_finished_spans()
        assert span.status.status_code == StatusCode.OK
        assert "Could not extract Pinecone request attributes (ValueError)" in caplog.text
        assert "instrumentation cannot count these" not in caplog.text
    finally:
        instrumentor.uninstrument()


def test_positional_namespace_and_tuple_vectors(monkeypatch, tracer_provider, in_memory_span_exporter):
    result = SimpleNamespace(upserted_count=1)

    def upsert(self, vectors, async_req=False, namespace=None):
        assert vectors == [("vec1", [0.1, 0.2, 0.3])]
        assert namespace == "test-ns"
        return result

    monkeypatch.setattr(Index, "upsert", upsert)
    instrumentor = PineconeInstrumentor()
    instrumentor.instrument(tracer_provider=tracer_provider)
    index = Index(api_key="test-not-a-secret", host="http://127.0.0.1:9")
    try:
        assert index.upsert([("vec1", [0.1, 0.2, 0.3])], False, "test-ns") is result
        span, = in_memory_span_exporter.get_finished_spans()
        assert span.attributes["db.vector.namespace"] == "test-ns"
        assert span.attributes["db.vector.upsert.count"] == 1
        assert span.attributes["db.vector.upsert.dimensions"] == 3
    finally:
        instrumentor.uninstrument()
        if hasattr(index, "close"):
            index.close()


@pytest.mark.parametrize("kind", [k for k in CLASSES if k == "async"])
@pytest.mark.asyncio
async def test_concurrent_async_parents(kind, monkeypatch, tracer_provider, in_memory_span_exporter):
    cls = CLASSES[kind]
    entered = []
    ready = asyncio.Event()

    async def query(self, *, namespace, nested=False, **kwargs):
        if nested:
            return SimpleNamespace(matches=[])
        entered.append(namespace)
        if len(entered) == 2:
            ready.set()
        await asyncio.wait_for(ready.wait(), timeout=2)
        return await self.query(namespace=namespace, nested=True)

    monkeypatch.setattr(cls, "query", query)
    instrumentor = PineconeInstrumentor()
    instrumentor.instrument(tracer_provider=tracer_provider)
    try:
        index = cls.__new__(cls)

        async def run(namespace):
            with tracer_provider.get_tracer("test").start_as_current_span(namespace):
                await index.query(namespace=namespace)

        await asyncio.gather(run("first"), run("second"))
        spans = in_memory_span_exporter.get_finished_spans()
        assert len(spans) == 4
        parents = {s.name: s for s in spans if s.name != "pinecone query"}
        for span in spans:
            if span.name == "pinecone query":
                assert span.parent.span_id == parents[span.attributes["db.vector.namespace"]].context.span_id
        # A completed await must not suppress the next, unrelated operation.
        await index.query(namespace="third")
        assert len(in_memory_span_exporter.get_finished_spans()) == 5
    finally:
        instrumentor.uninstrument()
