"""Offline tracing contract; SDK calls use synthetic method bodies/transports."""

import asyncio
import inspect
import socket
from unittest.mock import AsyncMock, Mock

import pymilvus
import pytest
from fi_instrumentation import TraceConfig, suppress_tracing
from opentelemetry import trace
from opentelemetry.exporter.otlp.proto.common.trace_encoder import encode_spans
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from opentelemetry.trace import SpanKind, StatusCode
from traceai_milvus import MilvusInstrumentor
from wrapt import FunctionWrapper

OPERATIONS = (
    "search",
    "query",
    "insert",
    "upsert",
    "delete",
    "get",
    "create_collection",
    "drop_collection",
    "hybrid_search",
    "search_iterator",
    "query_iterator",
)


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    def blocked(*args, **kwargs):
        raise AssertionError("Tests must not open network connections")

    monkeypatch.setattr(socket.socket, "connect", blocked)
    monkeypatch.setattr(socket, "create_connection", blocked)


@pytest.fixture
def telemetry():
    provider = TracerProvider()
    exporter = InMemorySpanExporter()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    instrumentor = MilvusInstrumentor()
    yield provider, exporter, instrumentor
    if instrumentor.is_instrumented_by_opentelemetry:
        instrumentor.uninstrument()
    provider.shutdown()


@pytest.fixture
def sdk(monkeypatch):
    class Pages:
        def __init__(self):
            self.calls = []
            self.batches = [[{"id": 1}], []]
            self.error = None
            self.marker = object()

        def next(self):
            self.calls.append("next")
            if self.error:
                raise self.error
            return self.batches.pop(0) if self.batches else []

        def close(self):
            self.calls.append("close")
            if self.error:
                raise self.error
            return self.marker

    class Client:
        def __init__(self):
            self.result = [[{"id": 1}, {"id": 2}]]
            self.error = None
            self.pages = Pages()
            self.active_span = None

        def search(
            self,
            collection_name,
            data=None,
            filter="",
            limit=10,
            output_fields=None,
            **kwargs,
        ):
            self.active_span = trace.get_current_span().get_span_context()
            if self.error:
                raise self.error
            return self.result

        query = insert = upsert = delete = get = create_collection = drop_collection = (
            search
        )
        hybrid_search = search

        def query_iterator(
            self, collection_name, batch_size=1000, limit=-1, filter="", **kwargs
        ):
            if self.error:
                raise self.error
            return self.pages

        search_iterator = query_iterator

    class AsyncClient(Client):
        async def search(
            self,
            collection_name,
            data=None,
            filter="",
            limit=10,
            output_fields=None,
            **kwargs,
        ):
            await asyncio.sleep(0)
            return super().search(
                collection_name, data, filter, limit, output_fields, **kwargs
            )

        query = insert = upsert = delete = get = create_collection = drop_collection = (
            search
        )
        hybrid_search = search

    monkeypatch.setattr(pymilvus, "MilvusClient", Client)
    monkeypatch.setattr(pymilvus, "AsyncMilvusClient", AsyncClient, raising=False)
    return Client, AsyncClient


def enable(telemetry, config=None):
    provider, exporter, instrumentor = telemetry
    instrumentor.instrument(tracer_provider=provider, config=config)
    return exporter, instrumentor


def test_restores_all_methods_and_reenable_is_single_span(sdk, telemetry):
    originals = {(cls, name): getattr(cls, name) for cls in sdk for name in OPERATIONS}
    exporter, instrumentor = enable(telemetry)
    client = sdk[0]()
    instrumentor.instrument(tracer_provider=telemetry[0])
    client.search("documents")
    assert len(exporter.get_finished_spans()) == 1
    instrumentor.uninstrument()
    for (cls, name), original in originals.items():
        assert getattr(cls, name) is original
    exporter.clear()
    client.search("documents")
    assert not exporter.get_finished_spans()
    instrumentor.instrument(tracer_provider=telemetry[0])
    client.search("documents")
    assert len(exporter.get_finished_spans()) == 1


@pytest.mark.parametrize("operation", OPERATIONS[:-2])
def test_sync_operation_shape_and_return_identity(sdk, telemetry, operation):
    exporter, _ = enable(telemetry)
    client = sdk[0]()
    result = getattr(client, operation)("documents", filter="id > 0")
    assert result is client.result
    (span,) = exporter.get_finished_spans()
    assert span.name == f"milvus {operation}"
    assert span.kind == SpanKind.CLIENT
    assert span.attributes["db.system"] == "milvus"
    assert span.attributes["db.namespace"] == "documents"
    assert span.status.status_code == StatusCode.OK
    assert span.context == client.active_span


@pytest.mark.parametrize("operation", OPERATIONS[:-2])
def test_async_awaited_result_and_single_span(sdk, telemetry, operation):
    exporter, _ = enable(telemetry)
    client = sdk[1]()
    with telemetry[0].get_tracer("test").start_as_current_span("parent") as parent:
        pending = getattr(client, operation)("documents")
        assert inspect.isawaitable(pending)
        assert not exporter.get_finished_spans()
        assert asyncio.run(pending) is client.result
        (span,) = exporter.get_finished_spans()
        assert span.parent.span_id == parent.get_span_context().span_id
        assert span.context == client.active_span
    assert span.name == f"milvus {operation}"
    assert span.attributes.get("db.vector.results.count") == (
        2
        if operation in {"search", "hybrid_search"}
        else 1
        if operation in {"query", "get"}
        else None
    )


@pytest.mark.parametrize(
    "config",
    [
        TraceConfig(hide_inputs=True),
        TraceConfig(hide_input_text=True),
        TraceConfig(hide_inputs=True, hide_error_details=True),
    ],
)
@pytest.mark.parametrize("asynchronous", [False, True])
def test_private_content_absent_from_otlp(sdk, telemetry, config, asynchronous):
    exporter, _ = enable(telemetry, config)
    client = sdk[int(asynchronous)]()
    error = ValueError("private-error-canary")
    client.error = error
    with pytest.raises(ValueError) as raised:
        result = client.search(
            "documents",
            filter="private-filter-canary",
            output_fields=["private-field-canary"],
        )
        if asynchronous:
            asyncio.run(result)
    assert raised.value is error
    (span,) = exporter.get_finished_spans()
    wire = encode_spans([span]).SerializeToString()
    for secret in (
        b"private-filter-canary",
        b"private-field-canary",
        b"private-error-canary",
        b"exception.message",
        b"exception.stacktrace",
    ):
        assert secret not in wire
    assert span.status.status_code == StatusCode.ERROR
    assert not span.status.description
    assert len(span.events) == 1
    assert span.events[0].attributes["exception.type"] == "ValueError"


def test_environment_privacy(sdk, telemetry, monkeypatch):
    monkeypatch.setenv("FI_HIDE_INPUTS", "true")
    monkeypatch.setenv("FI_HIDE_ERROR_DETAILS", "true")
    exporter, _ = enable(telemetry)
    client = sdk[0]()
    client.error = ValueError("private-error-canary")
    with pytest.raises(ValueError):
        client.search("documents", filter="private-filter-canary")
    assert (
        b"private-"
        not in encode_spans(exporter.get_finished_spans()).SerializeToString()
    )


def test_visible_errors_recorded_once_and_filters_preserved(sdk, telemetry):
    exporter, _ = enable(
        telemetry, TraceConfig(hide_inputs=False, hide_error_details=False)
    )
    client = sdk[0]()
    client.error = ValueError("synthetic failure")
    with pytest.raises(ValueError):
        client.search("documents", filter="id > 0")
    (span,) = exporter.get_finished_spans()
    assert span.attributes["db.vector.query.filter"] == "id > 0"
    assert len(span.events) == 1
    assert span.events[0].attributes["exception.message"] == "synthetic failure"
    assert span.status.status_code == StatusCode.ERROR


def test_positional_controls_and_empty_results(sdk, telemetry):
    exporter, _ = enable(telemetry)
    client = sdk[0]()
    client.result = []
    client.search("documents", [[0.1]], "id > 2", 7, ["id"])
    (span,) = exporter.get_finished_spans()
    assert span.attributes["db.vector.query.top_k"] == 7
    assert span.attributes["db.vector.query.filter"] == "id > 2"
    assert span.attributes["db.vector.query.output_fields"] == '["id"]'
    assert span.attributes["db.vector.results.count"] == 0


@pytest.mark.parametrize("operation", ["search_iterator", "query_iterator"])
def test_iterator_calls_are_lazy_bounded_and_disableable(sdk, telemetry, operation):
    exporter, instrumentor = enable(telemetry)
    client = sdk[0]()
    iterator = getattr(client, operation)("documents", filter="id > 0")
    assert client.pages.calls == []
    assert iterator.marker is client.pages.marker
    assert len(exporter.get_finished_spans()) == 1
    assert not trace.get_current_span().get_span_context().is_valid
    assert iterator.next() == [{"id": 1}]
    assert iterator.next() == []
    assert iterator.close() is client.pages.marker
    spans = exporter.get_finished_spans()
    assert [s.name for s in spans] == [
        f"milvus {operation}",
        f"milvus {operation}.next",
        f"milvus {operation}.next",
        f"milvus {operation}.close",
    ]
    assert [s.attributes.get("db.vector.results.count") for s in spans] == [
        None,
        1,
        0,
        None,
    ]
    assert all(s.end_time is not None for s in spans)
    instrumentor.uninstrument()
    iterator.next()
    iterator.close()
    assert len(exporter.get_finished_spans()) == 4


@pytest.mark.parametrize("method", ["next", "close"])
def test_iterator_errors_are_private_and_preserve_exception(sdk, telemetry, method):
    exporter, _ = enable(telemetry, TraceConfig(hide_inputs=True))
    client = sdk[0]()
    iterator = client.query_iterator("documents", filter="private-filter-canary")
    client.pages.error = ValueError("private-error-canary")
    with pytest.raises(ValueError) as raised:
        getattr(iterator, method)()
    assert raised.value is client.pages.error
    assert (
        b"private-"
        not in encode_spans(exporter.get_finished_spans()).SerializeToString()
    )
    assert exporter.get_finished_spans()[-1].status.status_code == StatusCode.ERROR


def test_suppression_does_not_change_results(sdk, telemetry):
    exporter, _ = enable(telemetry)
    client = sdk[0]()
    with suppress_tracing():
        assert client.search("documents") is client.result
        iterator = client.query_iterator("documents")
        iterator.next()
        iterator.close()
        assert asyncio.run(sdk[1]().search("documents"))
    assert not exporter.get_finished_spans()


def test_async_cancellation_ends_error_span(sdk, telemetry, monkeypatch):
    entered = asyncio.Event()

    async def pending(self, collection_name, **kwargs):
        entered.set()
        await asyncio.Event().wait()

    monkeypatch.setattr(sdk[1], "search", pending)
    exporter, _ = enable(telemetry, TraceConfig(hide_error_details=True))

    async def run():
        task = asyncio.create_task(sdk[1]().search("documents"))
        await entered.wait()
        assert not exporter.get_finished_spans()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert not trace.get_current_span().get_span_context().is_valid

    asyncio.run(run())
    (span,) = exporter.get_finished_spans()
    assert span.status.status_code == StatusCode.ERROR
    assert len(span.events) == 1
    assert span.end_time is not None


def test_later_wrapper_is_not_removed_and_old_wrapper_stays_disabled(sdk, telemetry):
    exporter, instrumentor = enable(telemetry)
    calls = []

    def other(wrapped, instance, args, kwargs):
        calls.append(1)
        return wrapped(*args, **kwargs)

    outer = FunctionWrapper(sdk[0].search, other)
    sdk[0].search = outer
    instrumentor.uninstrument()
    assert vars(sdk[0])["search"] is outer
    sdk[0]().search("documents")
    assert not exporter.get_finished_spans()
    instrumentor.instrument(tracer_provider=telemetry[0])
    sdk[0]().search("documents")
    assert len(exporter.get_finished_spans()) == 1
    assert calls == [1, 1]


def test_actual_sdk_search_and_query_without_server(telemetry, monkeypatch):
    original = pymilvus.MilvusClient.search
    client = object.__new__(pymilvus.MilvusClient)
    conn = Mock()
    conn.search.return_value = []
    conn.query.return_value = [{"id": 1}]
    monkeypatch.setattr(client, "_get_connection", lambda: conn)
    monkeypatch.setattr(
        client, "_generate_call_context", lambda **kw: None, raising=False
    )
    monkeypatch.setattr(client, "_with_cluster_id", lambda kw: kw, raising=False)
    exporter, instrumentor = enable(telemetry)
    assert client.search("documents", [[0.1, 0.2]], "id > 0", 7) == []
    assert client.query("documents", "id > 0", output_fields=["id"]) == [{"id": 1}]
    spans = exporter.get_finished_spans()
    assert len(spans) == 2
    assert spans[0].attributes["db.vector.query.top_k"] == 7
    assert spans[1].attributes["db.vector.results.count"] == 1
    assert "db.vector.query.top_k" not in spans[1].attributes
    instrumentor.uninstrument()
    assert pymilvus.MilvusClient.search is original


def test_actual_optional_methods_preserve_signature_and_restore(telemetry):
    classes = [pymilvus.MilvusClient]
    if hasattr(pymilvus, "AsyncMilvusClient"):
        classes.append(pymilvus.AsyncMilvusClient)
    originals = {
        (cls, name): getattr(cls, name)
        for cls in classes
        for name in OPERATIONS
        if hasattr(cls, name)
    }
    _, instrumentor = enable(telemetry)
    for (cls, name), original in originals.items():
        assert getattr(cls, name) is not original
        assert inspect.signature(getattr(cls, name)) == inspect.signature(original)
    instrumentor.uninstrument()
    for (cls, name), original in originals.items():
        assert getattr(cls, name) is original


def test_actual_async_sdk_without_server(telemetry, monkeypatch):
    if not hasattr(pymilvus, "AsyncMilvusClient"):
        pytest.skip("AsyncMilvusClient not provided by this SDK version")
    client = object.__new__(pymilvus.AsyncMilvusClient)
    conn = Mock()
    conn.search = AsyncMock(return_value=[[{"id": 1}]])
    conn.hybrid_search = AsyncMock(return_value=[[{"id": 2}]])
    monkeypatch.setattr(client, "_get_connection", AsyncMock(return_value=conn))
    monkeypatch.setattr(
        client, "_generate_call_context", lambda **kw: None, raising=False
    )
    monkeypatch.setattr(client, "_with_cluster_id", lambda kw: kw, raising=False)
    exporter, _ = enable(telemetry)
    assert asyncio.run(client.search("documents", [[0.1]], "id > 0", 7)) == [
        [{"id": 1}]
    ]
    assert asyncio.run(
        client.hybrid_search("documents", [], pymilvus.RRFRanker(), 3)
    ) == [[{"id": 2}]]
    assert [
        s.attributes["db.vector.query.top_k"] for s in exporter.get_finished_spans()
    ] == [7, 3]
    assert conn.search.await_count == conn.hybrid_search.await_count == 1


def test_hidden_inputs_never_reach_span_processor(sdk, telemetry):
    from opentelemetry.sdk.trace import SpanProcessor

    seen = []

    class Observer(SpanProcessor):
        def on_start(self, span, parent_context=None):
            seen.append(dict(span.attributes))

    telemetry[0].add_span_processor(Observer())
    enable(telemetry, TraceConfig(hide_inputs=True))
    sdk[0]().search(
        "documents",
        filter="private-filter-canary",
        output_fields=["private-field-canary"],
    )
    assert seen
    assert "private-" not in str(seen)


def test_concurrent_async_operations_have_separate_spans(sdk, telemetry):
    exporter, _ = enable(telemetry)

    async def run():
        clients = [sdk[1]() for _ in range(3)]
        await asyncio.gather(
            *(client.search(f"collection-{i}") for i, client in enumerate(clients))
        )

    asyncio.run(run())
    assert {s.attributes["db.namespace"] for s in exporter.get_finished_spans()} == {
        f"collection-{i}" for i in range(3)
    }
    assert len(exporter.get_finished_spans()) == 3


def test_saved_method_and_pending_coroutine_disabled(sdk, telemetry):
    exporter, instrumentor = enable(telemetry)
    client = sdk[0]()
    saved = client.search
    pending = sdk[1]().search("documents")
    instrumentor.uninstrument()
    assert saved("documents") is client.result
    asyncio.run(pending)
    assert not exporter.get_finished_spans()


def test_failed_instrumentation_rolls_back(sdk, telemetry, monkeypatch):
    import traceai_milvus

    original_wrapper = traceai_milvus.FunctionWrapper
    original_search = sdk[0].search

    def fail_query(wrapped, wrapper):
        if wrapper._operation == "query":
            raise RuntimeError("synthetic install failure")
        return original_wrapper(wrapped, wrapper)

    monkeypatch.setattr(traceai_milvus, "FunctionWrapper", fail_query)
    with pytest.raises(RuntimeError, match="synthetic install failure"):
        enable(telemetry)
    assert sdk[0].search is original_search
    assert not telemetry[2].is_instrumented_by_opentelemetry
    sdk[0]().search("documents")
    assert not telemetry[1].get_finished_spans()


def test_iterator_fetch_uses_callers_current_parent(sdk, telemetry):
    exporter, _ = enable(telemetry)
    iterator = sdk[0]().query_iterator("documents")
    with telemetry[0].get_tracer("test").start_as_current_span("consume") as parent:
        iterator.next()
    creation, fetch, consume = exporter.get_finished_spans()
    assert creation.parent is None
    assert fetch.parent.span_id == parent.get_span_context().span_id
    assert consume.name == "consume"


@pytest.mark.parametrize(
    "operation,key,args,kwargs,count",
    [
        ("insert", "db.vector.upsert.count", ([],), {}, 0),
        ("upsert", "db.vector.upsert.count", ({"id": 1},), {}, 1),
        ("delete", "db.vector.delete.count", (), {"ids": 0}, 1),
        ("delete", "db.vector.delete.count", (), {"pks": [1, 2]}, 2),
        ("get", "db.vector.query.ids_count", (), {"ids": []}, 0),
        ("create_collection", "db.vector.dimension", (), {"dimension": 128}, 128),
    ],
)
def test_counts_without_capturing_payloads(
    sdk, telemetry, operation, key, args, kwargs, count
):
    exporter, _ = enable(telemetry)
    getattr(sdk[0](), operation)("documents", *args, **kwargs)
    (span,) = exporter.get_finished_spans()
    assert span.attributes[key] == count


def test_actual_sync_hybrid_without_server(telemetry, monkeypatch):
    if not hasattr(pymilvus.MilvusClient, "hybrid_search"):
        pytest.skip("hybrid_search not provided by this SDK version")
    client = object.__new__(pymilvus.MilvusClient)
    conn = Mock()
    conn.hybrid_search.return_value = [[{"id": 1}]]
    monkeypatch.setattr(client, "_get_connection", lambda: conn)
    monkeypatch.setattr(
        client, "_generate_call_context", lambda **kw: None, raising=False
    )
    monkeypatch.setattr(client, "_with_cluster_id", lambda kw: kw, raising=False)
    exporter, _ = enable(telemetry, TraceConfig(hide_inputs=True))
    request = pymilvus.AnnSearchRequest(
        [[0.1]], "vector", {}, 2, expr="private-filter-canary"
    )
    result = client.hybrid_search("documents", [request], pymilvus.RRFRanker(), 3)
    assert result is conn.hybrid_search.return_value
    (span,) = exporter.get_finished_spans()
    assert span.name == "milvus hybrid_search"
    assert span.attributes["db.vector.query.top_k"] == 3
    assert span.attributes["db.vector.results.count"] == 1
    assert b"private-" not in encode_spans([span]).SerializeToString()
