"""Offline regressions against installed Weaviate SDK classes, not module mocks."""

import asyncio
import importlib
import importlib.metadata
import json
import socket
from http.server import BaseHTTPRequestHandler, HTTPServer
from threading import Thread
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
import requests
from fi_instrumentation import TraceConfig
from fi_instrumentation.instrumentation import suppress_tracing
from google.protobuf.json_format import MessageToDict
from opentelemetry import trace
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
from opentelemetry.proto.collector.trace.v1.trace_service_pb2 import (
    ExportTraceServiceRequest,
)
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from opentelemetry.trace import StatusCode
from traceai_weaviate import WeaviateInstrumentor
from weaviate.collections.classes.internal import GroupByReturn

QUERIES = {
    "near_vector": "_NearVectorQuery",
    "near_text": "_NearTextQuery",
    "hybrid": "_HybridQuery",
    "bm25": "_BM25Query",
    "fetch_objects": "_FetchObjectsQuery",
}
OPERATIONS = (*QUERIES, "insert", "insert_many", "delete_by_id", "delete_many")
SDK_VERSION = tuple(
    int(part) for part in importlib.metadata.version("weaviate-client").split(".")[:2]
)
MODES = [False, True] if SDK_VERSION >= (4, 7) else [False]
PRIVATE = "synthetic-private-weaviate-content"


def target(operation, is_async):
    """Find actual installed SDK descriptors across its three released layouts."""
    if operation in QUERIES:
        base = f"weaviate.collections.queries.{operation}.query"
        try:
            module = importlib.import_module(
                base + (".async_" if is_async else ".sync")
            )
        except ModuleNotFoundError:
            module = importlib.import_module(base)
        return getattr(module, QUERIES[operation] + ("Async" if is_async else ""))
    if SDK_VERSION < (4, 7):
        return importlib.import_module("weaviate.collections.data")._DataCollection
    if is_async:
        try:
            module = importlib.import_module("weaviate.collections.data.async_")
        except ModuleNotFoundError:
            module = importlib.import_module("weaviate.collections.data.data")
        return module._DataCollectionAsync
    return importlib.import_module("weaviate.collections.data.sync")._DataCollection


def invoke(client, operation, is_async, *args, **kwargs):
    result = getattr(client, operation)(*args, **kwargs)
    return asyncio.run(result) if is_async else result


@pytest.fixture(autouse=True)
def offline(monkeypatch):
    """No non-loopback socket can leave the tests, including in ordinary CI."""
    original = socket.socket.connect

    def connect(sock, address):
        if isinstance(address, tuple) and address[0] not in (
            "127.0.0.1",
            "::1",
            "localhost",
        ):
            raise AssertionError("Tests permit loopback sockets only")
        return original(sock, address)

    monkeypatch.setattr(socket.socket, "connect", connect)
    for key in (
        "FI_HIDE_INPUTS",
        "FI_HIDE_OUTPUTS",
        "FI_HIDE_ERROR_DETAILS",
        "FI_PII_REDACTION",
    ):
        monkeypatch.delenv(key, raising=False)


@pytest.fixture
def rig(monkeypatch):
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    instrumentor = WeaviateInstrumentor()
    if instrumentor.is_instrumented_by_opentelemetry:
        instrumentor.uninstrument()

    def setup(
        operation="near_text", is_async=False, result=None, error=None, **options
    ):
        cls = target(operation, is_async)
        calls = []

        def body(self, *args, **kwargs):
            calls.append((args, kwargs, trace.get_current_span().get_span_context()))
            if error is not None:
                raise error
            return result

        async def async_body(self, *args, **kwargs):
            await asyncio.sleep(0)
            return body(self, *args, **kwargs)

        original = async_body if is_async else body
        monkeypatch.setattr(cls, operation, original)
        instance = object.__new__(cls)
        instance._name = "Documents"
        instrumentor.instrument(tracer_provider=provider, **options)
        return SimpleNamespace(
            client=instance,
            cls=cls,
            original=original,
            calls=calls,
            provider=provider,
            exporter=exporter,
            instrumentor=instrumentor,
        )

    yield setup
    if instrumentor.is_instrumented_by_opentelemetry:
        instrumentor.uninstrument()
    provider.shutdown()


@pytest.mark.parametrize("is_async", MODES)
@pytest.mark.parametrize("operation", OPERATIONS)
def test_all_operations_preserve_results_arguments_and_parent(rig, operation, is_async):
    result = SimpleNamespace(objects=[], successful=0, failed=0, errors={})
    run = rig(operation, is_async, result=result)
    with run.provider.get_tracer("test").start_as_current_span("parent") as parent:
        assert (
            invoke(run.client, operation, is_async, PRIVATE, limit=3, distance=0)
            is result
        )
    spans = run.exporter.get_finished_spans()
    assert len(spans) == 2
    span = spans[0]
    assert span.name == f"weaviate {operation}"
    assert span.parent.span_id == parent.get_span_context().span_id
    assert run.calls[0][:2] == ((PRIVATE,), {"limit": 3, "distance": 0})
    assert run.calls[0][2].span_id == span.context.span_id
    assert span.kind is trace.SpanKind.CLIENT
    assert span.attributes["gen_ai.span.kind"] == (
        "RETRIEVER" if operation in QUERIES else "VECTOR_DB"
    )
    assert span.attributes["db.operation.name"] == operation
    assert span.attributes["db.namespace"] == "Documents"
    assert PRIVATE not in span.to_json()
    if operation in QUERIES:
        assert span.attributes["db.vector.query.top_k"] == 3
        assert span.attributes["db.vector.query.distance"] == 0
        assert span.attributes["db.vector.results.count"] == 0
    assert span.status.status_code is StatusCode.OK


@pytest.mark.parametrize("is_async", MODES)
@pytest.mark.parametrize("operation", OPERATIONS)
@pytest.mark.parametrize("hidden", [False, True])
def test_errors_preserve_identity_and_record_once(
    rig, monkeypatch, operation, is_async, hidden
):
    monkeypatch.setenv("FI_HIDE_ERROR_DETAILS", str(hidden).lower())
    error = ValueError(PRIVATE)
    run = rig(operation, is_async, error=error)
    with pytest.raises(ValueError) as caught:
        invoke(run.client, operation, is_async)
    assert caught.value is error
    spans = run.exporter.get_finished_spans()
    assert len(spans) == 1
    span = spans[0]
    assert span.status.status_code is StatusCode.ERROR
    assert len(span.events) == 1
    assert span.attributes["error.type"] == "ValueError"
    assert (PRIVATE not in span.to_json()) is hidden
    if hidden:
        assert span.status.description is None
        assert "exception.stacktrace" not in span.events[0].attributes


@pytest.mark.parametrize("is_async", MODES)
def test_suppression(rig, is_async):
    run = rig(is_async=is_async, result=PRIVATE)
    with suppress_tracing():
        assert invoke(run.client, "near_text", is_async, query=PRIVATE) == PRIVATE
    assert not run.exporter.get_finished_spans()
    assert len(run.calls) == 1


@pytest.mark.parametrize("is_async", MODES)
def test_disable_reenable_and_repeated_instrumentation(rig, is_async):
    run = rig(is_async=is_async)
    run.instrumentor.instrument(tracer_provider=run.provider)
    invoke(run.client, "near_text", is_async)
    assert len(run.exporter.get_finished_spans()) == 1
    run.exporter.clear()
    run.instrumentor.uninstrument()
    assert getattr(run.cls, "near_text") is run.original
    invoke(run.client, "near_text", is_async)
    assert not run.exporter.get_finished_spans()
    run.instrumentor.instrument(tracer_provider=run.provider)
    invoke(run.client, "near_text", is_async)
    assert len(run.exporter.get_finished_spans()) == 1


def test_restore_inherited_descriptors(rig):
    cls = target("delete_by_id", False)
    before = dict(cls.__dict__)
    run = rig()
    run.instrumentor.uninstrument()
    assert cls.__dict__.get("delete_by_id") is before.get("delete_by_id")
    assert ("delete_by_id" in cls.__dict__) == ("delete_by_id" in before)


@pytest.mark.parametrize("is_async", MODES)
def test_no_invented_limit_or_alpha(rig, is_async):
    run = rig("hybrid", is_async)
    invoke(run.client, "hybrid", is_async, PRIVATE)
    attributes = run.exporter.get_finished_spans()[0].attributes
    assert "db.vector.query.top_k" not in attributes
    assert "db.vector.query.hybrid.alpha" not in attributes


@pytest.mark.parametrize("is_async", MODES)
@pytest.mark.parametrize("hidden", [False, True])
def test_opt_in_content_honors_privacy(rig, is_async, hidden):
    result = SimpleNamespace(
        objects=[SimpleNamespace(properties={"text": PRIVATE}, vector=[123.456])]
    )
    run = rig(
        is_async=is_async,
        result=result,
        capture_content=True,
        config=TraceConfig(hide_inputs=hidden, hide_outputs=hidden),
    )
    invoke(run.client, "near_text", is_async, PRIVATE)
    span = run.exporter.get_finished_spans()[0]
    assert (PRIVATE not in span.to_json()) is hidden
    assert "123.456" not in span.to_json()
    if not hidden:
        assert span.attributes["gen_ai.retrieval.query"] == PRIVATE
        assert json.loads(
            span.attributes["retrieval.documents.0.document.content"]
        ) == {"text": PRIVATE}


@pytest.mark.parametrize(
    "flag",
    [
        "hide_input_text",
        "hide_input_messages",
        "hide_output_text",
        "hide_output_messages",
    ],
)
def test_text_privacy_flags(rig, flag):
    result = SimpleNamespace(
        objects=[SimpleNamespace(properties={"text": "private-output"})]
    )
    run = rig(result=result, capture_content=True, config=TraceConfig(**{flag: True}))
    run.client.near_text("private-input")
    span = run.exporter.get_finished_spans()[0]
    assert ("private-input" not in span.to_json()) is flag.startswith("hide_input")
    assert ("private-output" not in span.to_json()) is flag.startswith("hide_output")


def test_content_off_by_default_and_bounded_when_enabled(rig):
    result = SimpleNamespace(
        objects=[SimpleNamespace(properties={"text": PRIVATE})] * 30
    )
    run = rig(result=result)
    run.client.near_text(PRIVATE)
    assert PRIVATE not in run.exporter.get_finished_spans()[0].to_json()
    run.instrumentor.uninstrument()
    run.instrumentor.instrument(tracer_provider=run.provider, capture_content=True)
    run.client.near_text("x" * 20_000)
    span = run.exporter.get_finished_spans()[-1]
    assert len(span.attributes["gen_ai.retrieval.query"]) == 16_384
    assert "retrieval.documents.19.document.content" in span.attributes
    assert "retrieval.documents.20.document.content" not in span.attributes


@pytest.mark.parametrize("is_async", MODES)
@pytest.mark.parametrize(
    "operation,result,count_key,count",
    [
        (
            "insert_many",
            SimpleNamespace(errors={0: PRIVATE}),
            "db.vector.errors.count",
            1,
        ),
        (
            "delete_many",
            SimpleNamespace(successful=2, failed=1),
            "db.vector.delete.count",
            2,
        ),
        ("delete_by_id", False, "db.vector.delete.count", 0),
        (
            "fetch_objects",
            SimpleNamespace(groups={"a": SimpleNamespace(objects=[1, 2])}),
            "db.vector.results.count",
            2,
        ),
    ],
)
def test_counts_and_partial_failures(
    rig, operation, result, count_key, count, is_async
):
    run = rig(operation, is_async, result=result)
    assert invoke(run.client, operation, is_async) is result
    span = run.exporter.get_finished_spans()[0]
    assert span.attributes[count_key] == count
    assert PRIVATE not in span.to_json()
    expected = (
        StatusCode.ERROR
        if operation in ("insert_many", "delete_many")
        else StatusCode.OK
    )
    assert span.status.status_code is expected


@pytest.mark.parametrize("is_async", MODES)
@pytest.mark.parametrize("objects_are_flattened", [False, True])
def test_grouped_query_counts_objects_not_groups(rig, is_async, objects_are_flattened):
    groups = {
        "a": SimpleNamespace(objects=[SimpleNamespace(properties={}) for _ in range(3)]),
        "b": SimpleNamespace(objects=[SimpleNamespace(properties={}) for _ in range(4)]),
    }
    # The SDK flattens groups into objects; also guard against a response with
    # an incomplete top-level objects list so the group count stays authoritative.
    objects = [obj for group in groups.values() for obj in group.objects]
    response = GroupByReturn(
        objects=objects if objects_are_flattened else objects[:2], groups=groups
    )
    run = rig("near_text", is_async, result=response)
    assert invoke(run.client, "near_text", is_async, query="synthetic") is response
    (span,) = run.exporter.get_finished_spans()
    assert span.attributes["db.vector.results.count"] == 7
    assert len(response.groups) == 2


@pytest.mark.skipif(True not in MODES, reason="SDK predates async client")
def test_cancellation_finishes_span(rig):
    run = rig(
        is_async=True,
        error=asyncio.CancelledError(PRIVATE),
        config=TraceConfig(hide_error_details=True),
    )
    with pytest.raises(asyncio.CancelledError):
        invoke(run.client, "near_text", True)
    (span,) = run.exporter.get_finished_spans()
    assert span.status.status_code is StatusCode.ERROR
    assert len(span.events) == 1
    assert PRIVATE not in span.to_json()


def test_telemetry_failure_does_not_replace_result(rig, monkeypatch, caplog):
    run = rig(result=PRIVATE)
    from traceai_weaviate._wrappers import OperationWrapper

    monkeypatch.setattr(
        OperationWrapper, "_attributes", Mock(side_effect=RuntimeError(PRIVATE))
    )
    with caplog.at_level("DEBUG", logger="traceai_weaviate._wrappers"):
        assert run.client.near_text() == PRIVATE
    assert len(run.calls) == 1
    assert "span creation failed" in caplog.text
    assert PRIVATE not in caplog.text


@pytest.fixture
def collector():
    bodies = []

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            assert self.path == "/v1/traces"
            bodies.append(self.rfile.read(int(self.headers["Content-Length"])))
            self.send_response(200)
            self.end_headers()

        def log_message(self, *args):
            pass

    server = HTTPServer(("127.0.0.1", 0), Handler)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{server.server_port}/v1/traces", bodies
    server.shutdown()
    server.server_close()
    thread.join(timeout=5)


@pytest.mark.parametrize("is_async", MODES)
@pytest.mark.parametrize(
    "privacy_flag", ["FI_HIDE_ERROR_DETAILS", "FI_HIDE_INPUTS", "FI_HIDE_OUTPUTS"]
)
def test_otlp_wire_privacy_and_shape(
    rig, collector, monkeypatch, is_async, privacy_flag
):
    monkeypatch.setenv(privacy_flag, "true")
    run = rig(is_async=is_async, error=ValueError(PRIVATE))
    endpoint, bodies = collector
    session = requests.Session()
    session.trust_env = False
    run.provider.add_span_processor(
        SimpleSpanProcessor(
            OTLPSpanExporter(
                endpoint=endpoint,
                headers={},
                session=session,
                timeout=2,
            )
        )
    )
    with pytest.raises(ValueError):
        invoke(run.client, "near_text", is_async, PRIVATE)
    assert len(bodies) == 1
    request = ExportTraceServiceRequest.FromString(bodies[0])
    wire = json.dumps(MessageToDict(request))
    assert PRIVATE not in wire
    (span,) = request.resource_spans[0].scope_spans[0].spans
    attributes = {item.key: item.value.string_value for item in span.attributes}
    assert attributes["gen_ai.span.kind"] == "RETRIEVER"
    assert attributes["error.type"] == "ValueError"
    assert span.status.code == 2
    assert not span.status.message
    assert len(span.events) == 1
    assert all(
        item.key not in ("exception.message", "exception.stacktrace")
        for item in span.events[0].attributes
    )


@pytest.mark.parametrize("is_async", MODES)
def test_public_collections_with_real_sdk_and_fake_transports(monkeypatch, is_async):
    """Run SDK validation, request construction, syncify/executor and parsing."""
    import httpx
    import weaviate
    from weaviate.connect import ConnectionParams
    from weaviate.proto.v1 import search_get_pb2
    from weaviate.util import _ServerVersion

    SearchReply = search_get_pb2.SearchReply

    event_loop = asyncio.new_event_loop()
    asyncio.set_event_loop(event_loop)
    client_type = weaviate.WeaviateAsyncClient if is_async else weaviate.WeaviateClient
    client = client_type(
        connection_params=ConnectionParams.from_url(
            "http://127.0.0.1:9", grpc_port=50051
        ),
        skip_init_checks=True,
    )
    connection = client._connection
    connection._weaviate_version = _ServerVersion.from_string("1.27.0")
    monkeypatch.setattr(connection, "is_connected", lambda: True)
    monkeypatch.setattr(connection, "get_current_bearer_token", lambda: "")
    # Between 4.7 and the executor rewrite, even the sync client has async I/O.
    transport_async = is_async or (
        SDK_VERSION >= (4, 7) and not hasattr(connection, "grpc_search")
    )
    search = (
        AsyncMock(return_value=SearchReply())
        if transport_async
        else Mock(return_value=SearchReply())
    )
    search.with_call = Mock(return_value=(SearchReply(), None))
    connection._grpc_stub = SimpleNamespace(Search=search)
    post = (
        AsyncMock(return_value=httpx.Response(200))
        if transport_async
        else Mock(return_value=httpx.Response(200))
    )
    monkeypatch.setattr(connection, "post", post)
    collection = client.collections.get("Documents")
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    instrumentor = WeaviateInstrumentor()
    instrumentor.instrument(
        tracer_provider=provider, config=TraceConfig(hide_error_details=True)
    )
    try:
        with provider.get_tracer("test").start_as_current_span("parent") as parent:
            response = invoke(
                collection.query, "near_text", is_async, "semantic search", limit=3
            )
            assert response.objects == []
            uuid = "d1e27d9a-247a-4cbb-902b-72b58789c17e"
            assert (
                str(
                    invoke(
                        collection.data,
                        "insert",
                        is_async,
                        {"text": PRIVATE},
                        uuid=uuid,
                    )
                )
                == uuid
            )
        query, insert, _ = exporter.get_finished_spans()
        assert (
            query.parent.span_id
            == insert.parent.span_id
            == parent.get_span_context().span_id
        )
        assert query.attributes["db.vector.results.count"] == 0
        assert insert.attributes["gen_ai.span.kind"] == "VECTOR_DB"
        assert PRIVATE not in insert.to_json()
        call = search.with_call.call_args if SDK_VERSION < (4, 7) else search.call_args
        assert call.args[0].limit == 3
        assert list(call.args[0].near_text.query) == ["semantic search"]
        assert post.call_args.kwargs["weaviate_object"]["properties"] == {
            "text": PRIVATE
        }
        exporter.clear()
        with suppress_tracing():
            invoke(collection.query, "near_text", is_async, "suppressed")
        assert not exporter.get_finished_spans()
        instrumentor.uninstrument()
        invoke(collection.query, "near_text", is_async, "disabled")
        assert not exporter.get_finished_spans()
        instrumentor.instrument(
            tracer_provider=provider, config=TraceConfig(hide_error_details=True)
        )
        error = ValueError(PRIVATE)
        search.side_effect = search.with_call.side_effect = error
        with pytest.raises(ValueError) as caught:
            invoke(collection.query, "near_text", is_async, "failure")
        assert caught.value is error
        (failed,) = exporter.get_finished_spans()
        assert len(failed.events) == 1
        assert PRIVATE not in failed.to_json()
    finally:
        instrumentor.uninstrument()
        provider.shutdown()
        connection._grpc_stub = None
        if is_async:
            asyncio.run(client.close())
        else:
            client.close()
        event_loop.close()
        asyncio.set_event_loop(None)


def test_failed_install_rolls_back_and_can_retry(monkeypatch):
    import traceai_weaviate as instrumentation

    targets = instrumentation._targets()
    before = [(cls, name, dict(cls.__dict__)) for cls, name, _ in targets]
    real_wrapper = instrumentation.FunctionWrapper
    count = 0

    def fail_during_install(*args):
        nonlocal count
        count += 1
        if count == 3:
            raise RuntimeError("synthetic install failure")
        return real_wrapper(*args)

    instrumentor = WeaviateInstrumentor()
    monkeypatch.setattr(instrumentation, "FunctionWrapper", fail_during_install)
    with pytest.raises(RuntimeError, match="synthetic install failure"):
        instrumentor.instrument()
    assert not instrumentor.is_instrumented_by_opentelemetry
    for cls, name, attributes in before:
        assert cls.__dict__.get(name) is attributes.get(name)
        assert (name in cls.__dict__) == (name in attributes)
    monkeypatch.setattr(instrumentation, "FunctionWrapper", real_wrapper)
    instrumentor.instrument()
    instrumentor.uninstrument()


@pytest.mark.skipif(True not in MODES, reason="SDK predates async client")
def test_concurrent_async_calls_keep_separate_parents(rig):
    run = rig(is_async=True, result=PRIVATE)

    async def call(index):
        with run.provider.get_tracer("test").start_as_current_span(f"parent-{index}"):
            assert await run.client.near_text(query=PRIVATE) == PRIVATE

    async def both():
        await asyncio.gather(call(0), call(1))

    asyncio.run(both())
    spans = run.exporter.get_finished_spans()
    parents = {
        span.context.span_id for span in spans if span.name.startswith("parent-")
    }
    children = [span for span in spans if span.name == "weaviate near_text"]
    assert len(children) == len(parents) == 2
    assert {span.parent.span_id for span in children} == parents
    assert not trace.get_current_span().get_span_context().is_valid


@pytest.mark.parametrize("is_async", MODES)
@pytest.mark.parametrize("hidden", [False, True])
def test_otlp_wire_content_policy(rig, collector, is_async, hidden):
    result = SimpleNamespace(objects=[SimpleNamespace(properties={"text": PRIVATE})])
    run = rig(
        is_async=is_async,
        result=result,
        capture_content=True,
        config=TraceConfig(hide_inputs=hidden, hide_outputs=hidden),
    )
    endpoint, bodies = collector
    session = requests.Session()
    session.trust_env = False
    run.provider.add_span_processor(
        SimpleSpanProcessor(
            OTLPSpanExporter(
                endpoint=endpoint,
                headers={},
                session=session,
                timeout=2,
            )
        )
    )
    assert invoke(run.client, "near_text", is_async, PRIVATE) is result
    request = ExportTraceServiceRequest.FromString(bodies[0])
    wire = json.dumps(MessageToDict(request))
    assert (PRIVATE not in wire) is hidden
    (span,) = request.resource_spans[0].scope_spans[0].spans
    (count,) = [
        item.value.int_value
        for item in span.attributes
        if item.key == "db.vector.results.count"
    ]
    assert count == 1
