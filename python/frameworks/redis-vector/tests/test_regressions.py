"""Synthetic regressions using real Redis/RedisVL APIs, with no Redis server."""

import asyncio
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from threading import Thread
from unittest.mock import PropertyMock

import pytest
from fi_instrumentation import TraceConfig
from opentelemetry import trace
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
from opentelemetry.instrumentation.redis import RedisInstrumentor as CommandInstrumentor
from opentelemetry.instrumentation.utils import suppress_instrumentation
from opentelemetry.proto.collector.trace.v1.trace_service_pb2 import (
    ExportTraceServiceRequest,
)
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from redis import Redis
from redis.asyncio import Redis as AsyncRedis
from redis.commands.search import AsyncSearch, Search
from redis.commands.search.field import TextField
from redis.commands.search.query import Query
from redis.commands.search.result import Result
from redisvl.index import AsyncSearchIndex, SearchIndex
from redisvl.query import VectorQuery
from traceai_redis import RedisInstrumentor
from wrapt import wrap_function_wrapper

SCHEMA = {
    "index": {"name": "documents", "prefix": "doc"},
    "fields": [
        {"name": "content", "type": "text"},
        {
            "name": "embedding",
            "type": "vector",
            "attrs": {
                "dims": 3,
                "algorithm": "flat",
                "datatype": "float32",
                "distance_metric": "cosine",
            },
        },
    ],
}
SECRET = "synthetic-private-query-123"


@pytest.fixture
def telemetry():
    provider = TracerProvider()
    exporter = InMemorySpanExporter()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    instrumentor = RedisInstrumentor()
    yield instrumentor, provider, exporter
    if instrumentor.is_instrumented_by_opentelemetry:
        instrumentor.uninstrument()
    provider.shutdown()


@pytest.fixture
def clients(monkeypatch):
    """Stub only I/O/client validation; run real query/search/result processing."""
    sync = Redis(protocol=2)
    async_client = AsyncRedis(protocol=2)

    def execute(self, *args, **kwargs):
        if args[0] == "FT.SEARCH":
            return [1, b"doc:1", [b"content", b"safe", b"vector_distance", b"0.1"]]
        return b"OK"

    async def async_execute(self, *args, **kwargs):
        await asyncio.sleep(0)
        return execute(self, *args, **kwargs)

    async def get_client(self):
        return async_client

    async def exists(self):
        return True

    monkeypatch.setattr(Redis, "execute_command", execute)
    monkeypatch.setattr(AsyncRedis, "execute_command", async_execute)
    monkeypatch.setattr(
        SearchIndex, "_redis_client", PropertyMock(return_value=sync), raising=False
    )
    monkeypatch.setattr(
        AsyncSearchIndex,
        "_redis_client",
        PropertyMock(return_value=async_client),
        raising=False,
    )
    monkeypatch.setattr(AsyncSearchIndex, "_get_client", get_client, raising=False)
    monkeypatch.setattr(SearchIndex, "exists", lambda self: True)
    monkeypatch.setattr(AsyncSearchIndex, "exists", exists)
    return sync, async_client


@pytest.mark.parametrize("method", ["search", "query"])
@pytest.mark.parametrize("asynchronous", [False, True])
def test_real_redisvl_result_and_nested_search(
    telemetry, clients, method, asynchronous
):
    instrumentor, provider, exporter = telemetry
    cls = AsyncSearchIndex if asynchronous else SearchIndex
    index = cls.from_dict(SCHEMA)
    query = VectorQuery(
        [0.1, 0.2, 0.3], "embedding", return_fields=["content"], num_results=2
    )
    value = query if method == "query" else query.query
    kwargs = {} if method == "query" else {"query_params": query.params}
    instrumentor.instrument(tracer_provider=provider)
    if asynchronous:
        result = asyncio.run(getattr(index, method)(value, **kwargs))
    else:
        result = getattr(index, method)(value, **kwargs)
    assert isinstance(result, list if method == "query" else Result)
    spans = exporter.get_finished_spans()
    assert len(spans) == 1  # query -> RedisVL.search -> redis-py.search
    span = spans[0]
    assert span.name == ("redis query" if method == "query" else "redis vector_search")
    assert span.attributes["db.namespace"] == "documents"
    assert span.attributes["db.vector.results.count"] == 1
    if method == "query":
        assert span.attributes["db.vector.query.top_k"] == 2
        assert span.attributes["db.vector.query.field"] == "embedding"
        assert span.attributes["db.vector.query.type"] == "knn"
    assert span.status.status_code is trace.StatusCode.OK


@pytest.mark.parametrize("asynchronous", [False, True])
@pytest.mark.parametrize("method", ["search", "create_index", "dropindex"])
def test_real_redis_search_apis(telemetry, clients, asynchronous, method):
    instrumentor, provider, exporter = telemetry
    instrumentor.instrument(tracer_provider=provider)
    client = clients[int(asynchronous)]
    search = client.ft("documents")
    args = {
        "search": [Query("*")],
        "create_index": [[TextField("content")]],
        "dropindex": [],
    }[method]
    result = getattr(search, method)(*args)
    if asynchronous:
        result = asyncio.run(result)
    assert isinstance(result, Result) if method == "search" else result == b"OK"
    spans = exporter.get_finished_spans()
    assert len(spans) == 1
    assert (
        spans[0].name
        == "redis "
        + {
            "search": "ft_search",
            "create_index": "ft_create",
            "dropindex": "ft_dropindex",
        }[method]
    )
    assert spans[0].status.status_code is trace.StatusCode.OK


@pytest.mark.parametrize("asynchronous", [False, True])
def test_independent_nested_search_is_traced(telemetry, monkeypatch, asynchronous):
    instrumentor, provider, exporter = telemetry
    cls = AsyncSearchIndex if asynchronous else SearchIndex
    index = cls.from_dict(SCHEMA)

    def search(self, *args, **kwargs):
        return []

    async def asearch(self, *args, **kwargs):
        await asyncio.sleep(0)
        return []

    def load(self, *args, **kwargs):
        assert self.search(Query("*")) == []
        return ["doc:1"]

    async def aload(self, *args, **kwargs):
        assert await self.search(Query("*")) == []
        return ["doc:1"]

    monkeypatch.setattr(cls, "search", asearch if asynchronous else search)
    monkeypatch.setattr(cls, "load", aload if asynchronous else load)
    instrumentor.instrument(tracer_provider=provider)
    result = index.load([{"content": "safe"}])
    if asynchronous:
        result = asyncio.run(result)
    assert result == ["doc:1"]
    child, parent = exporter.get_finished_spans()
    assert child.name == "redis vector_search"
    assert parent.name == "redis load"
    assert child.parent.span_id == parent.context.span_id


@pytest.mark.parametrize("asynchronous", [False, True])
def test_independent_ft_search_inside_redisvl_search_is_traced(
    telemetry, clients, monkeypatch, asynchronous
):
    instrumentor, provider, exporter = telemetry
    cls = AsyncSearchIndex if asynchronous else SearchIndex
    client = clients[int(asynchronous)]

    def search(self, *args, **kwargs):
        independent = client.ft("independent-index").search(Query("*"))
        expected = client.ft("documents").search(Query("*"))
        assert independent.total == expected.total == 1
        return expected

    async def asearch(self, *args, **kwargs):
        independent = await client.ft("independent-index").search(Query("*"))
        expected = await client.ft("documents").search(Query("*"))
        assert independent.total == expected.total == 1
        return expected

    monkeypatch.setattr(cls, "search", asearch if asynchronous else search)
    instrumentor.instrument(tracer_provider=provider)
    result = cls.from_dict(SCHEMA).search(Query("*"))
    if asynchronous:
        result = asyncio.run(result)
    assert result.total == 1
    child, parent = exporter.get_finished_spans()
    assert (child.name, child.attributes["db.namespace"]) == (
        "redis ft_search",
        "independent-index",
    )
    assert (parent.name, parent.attributes["db.namespace"]) == (
        "redis vector_search",
        "documents",
    )
    assert child.parent.span_id == parent.context.span_id


def test_readme_qualifies_lower_level_search_result_shape():
    readme = (Path(__file__).resolve().parents[1] / "README.md").read_text()
    assert "With the default RESP2 client" in readme
    assert "RESP3 client can return a raw mapping" in readme


def test_partial_instrumentation_failure_restores_patches(telemetry, monkeypatch):
    instrumentor, provider, _ = telemetry
    search = vars(SearchIndex)["search"]
    monkeypatch.delattr(SearchIndex, "query")
    with pytest.raises(AttributeError):
        instrumentor.instrument(tracer_provider=provider)
    assert not instrumentor.is_instrumented_by_opentelemetry
    assert vars(SearchIndex)["search"] is search
    assert not instrumentor._originals
    monkeypatch.setattr(SearchIndex, "query", lambda self, *args: [], raising=False)
    instrumentor.instrument(tracer_provider=provider)
    instrumentor.uninstrument()
    assert vars(SearchIndex)["search"] is search


@pytest.mark.parametrize("asynchronous", [False, True])
@pytest.mark.parametrize("knn", [False, True])
def test_direct_redis_py_knn_classification_on_wire(
    telemetry, clients, asynchronous, knn
):
    instrumentor, provider, _ = telemetry
    query = Query(
        "*=>[KNN 2 @embedding $vec AS score]" if knn else "synthetic-private-query-123"
    ).dialect(2)
    with wire_exporter() as (exporter, payloads):
        provider.add_span_processor(SimpleSpanProcessor(exporter))
        instrumentor.instrument(tracer_provider=provider)
        result = clients[int(asynchronous)].ft("documents").search(query)
        if asynchronous:
            result = asyncio.run(result)
        assert isinstance(result, Result)
        assert len(payloads) == 1
        assert SECRET.encode() not in payloads[0]
        request = ExportTraceServiceRequest.FromString(payloads[0])
        (span,) = request.resource_spans[0].scope_spans[0].spans
        attributes = {a.key: a.value for a in span.attributes}
        assert ("db.vector.query.type" in attributes) is knn
        if knn:
            assert attributes["db.vector.query.type"].string_value == "knn"


@pytest.mark.parametrize(
    "result", [Result([0], hascontent=True), [], {}, None, object()]
)
@pytest.mark.parametrize("asynchronous", [False, True])
def test_return_identity_and_empty_results(
    telemetry, monkeypatch, result, asynchronous
):
    instrumentor, provider, exporter = telemetry
    cls = AsyncSearchIndex if asynchronous else SearchIndex

    def successful(self, *args, **kwargs):
        return result

    async def successful_async(self, *args, **kwargs):
        await asyncio.sleep(0)
        return result

    monkeypatch.setattr(cls, "search", successful_async if asynchronous else successful)
    instrumentor.instrument(tracer_provider=provider)
    index = cls.from_dict(SCHEMA)
    returned = index.search(Query("*"))
    if asynchronous:
        returned = asyncio.run(returned)
    assert returned is result
    (span,) = exporter.get_finished_spans()
    if isinstance(result, (Result, list)):
        assert span.attributes["db.vector.results.count"] == 0
    else:
        assert "db.vector.results.count" not in span.attributes


def test_metadata_failures_do_not_change_results_or_log_content(
    telemetry, monkeypatch, caplog
):
    instrumentor, provider, exporter = telemetry

    class UnusualResult:
        @property
        def docs(self):
            raise ValueError(SECRET)

    result = UnusualResult()
    monkeypatch.setattr(SearchIndex, "search", lambda *args, **kwargs: result)
    instrumentor.instrument(tracer_provider=provider)
    with caplog.at_level("DEBUG", logger="traceai_redis._wrappers"):
        assert SearchIndex.from_dict(SCHEMA).search(Query("*")) is result
    assert "Redis result metadata extraction failed" in caplog.text
    assert SECRET not in caplog.text
    assert len(exporter.get_finished_spans()) == 1


@pytest.mark.parametrize("asynchronous", [False, True])
def test_real_load_keeps_generator_and_return(
    telemetry, clients, monkeypatch, asynchronous
):
    instrumentor, provider, exporter = telemetry
    cls = AsyncSearchIndex if asynchronous else SearchIndex
    index = cls.from_dict(SCHEMA)
    data = (row for row in [{"content": "safe"}])
    keys = ["doc:1"]

    def write(self, client, *, objects, **kwargs):
        assert objects is data
        assert list(objects) == [{"content": "safe"}]
        # Do not suppress unrelated instrumentation, e.g. a user's preprocessor.
        with provider.get_tracer("preprocessor").start_as_current_span("preprocess"):
            pass
        return keys

    async def awrite(self, client, **kwargs):
        await asyncio.sleep(0)
        return write(self, client, **kwargs)

    monkeypatch.setattr(type(index._storage), "write", write)
    monkeypatch.setattr(type(index._storage), "awrite", awrite)
    instrumentor.instrument(tracer_provider=provider)
    result = index.load(data)
    if asynchronous:
        result = asyncio.run(result)
    assert result is keys
    spans = exporter.get_finished_spans()
    span = next(span for span in spans if span.name == "redis load")
    assert span.attributes["db.vector.upsert.count"] == 1
    assert len(spans) == 2
    assert spans[0].parent.span_id == span.context.span_id


@pytest.mark.parametrize("asynchronous", [False, True])
def test_suppression_and_parenting(telemetry, clients, asynchronous):
    instrumentor, provider, exporter = telemetry
    instrumentor.instrument(tracer_provider=provider)

    async def run_async():
        with suppress_instrumentation():
            assert isinstance(
                await clients[1].ft("documents").search(Query("*")), Result
            )
        with provider.get_tracer("test").start_as_current_span("parent"):
            await clients[1].ft("documents").search(Query("*"))

    if asynchronous:
        asyncio.run(run_async())
    else:
        with suppress_instrumentation():
            assert isinstance(clients[0].ft("documents").search(Query("*")), Result)
        with provider.get_tracer("test").start_as_current_span("parent"):
            clients[0].ft("documents").search(Query("*"))
    child, parent = exporter.get_finished_spans()
    assert child.parent.span_id == parent.context.span_id
    assert child.context.trace_id == parent.context.trace_id


def test_uninstrument_restores_inherited_and_owned_methods(telemetry):
    instrumentor, provider, _ = telemetry
    targets = [
        (cls, method)
        for cls in (SearchIndex, AsyncSearchIndex)
        for method in ("search", "query", "load")
    ]
    targets += [
        (cls, method)
        for cls in (Search, AsyncSearch)
        for method in ("search", "create_index", "dropindex")
    ]
    missing = object()
    originals = [
        (cls, name, vars(cls).get(name, missing), getattr(cls, name))
        for cls, name in targets
    ]
    for _ in range(2):
        instrumentor.instrument(tracer_provider=provider)
        instrumentor.instrument(
            tracer_provider=provider
        )  # BaseInstrumentor is idempotent.
        for cls, name, _, original in originals:
            assert getattr(cls, name) is not original
        instrumentor.uninstrument()
        for cls, name, local, original in originals:
            assert vars(cls).get(name, missing) is local
            assert getattr(cls, name) is original


def test_uninstrument_does_not_clobber_later_application_patch(telemetry, clients):
    instrumentor, provider, exporter = telemetry
    original = vars(Search).get("search")
    instrumentor.instrument(tracer_provider=provider)
    calls = []

    def application_wrapper(wrapped, instance, args, kwargs):
        calls.append(True)
        return wrapped(*args, **kwargs)

    wrap_function_wrapper(Search, "search", application_wrapper)
    foreign_patch = vars(Search)["search"]
    try:
        instrumentor.uninstrument()
        assert vars(Search)["search"] is foreign_patch
        clients[0].ft("documents").search(Query("*"))
        assert calls == [True]
        assert not exporter.get_finished_spans()
    finally:
        if original is None:
            delattr(Search, "search")
        else:
            Search.search = original


@pytest.mark.parametrize("asynchronous", [False, True])
@pytest.mark.parametrize("command_first", [False, True])
def test_maintained_command_instrumentation_does_not_duplicate_search(
    telemetry, clients, asynchronous, command_first
):
    instrumentor, provider, exporter = telemetry
    command = CommandInstrumentor()
    try:
        if command_first:
            command.instrument(tracer_provider=provider)
        instrumentor.instrument(tracer_provider=provider)
        if not command_first:
            command.instrument(tracer_provider=provider)
        if asynchronous:
            asyncio.run(clients[1].ft("documents").search(Query("*")))
        else:
            clients[0].ft("documents").search(Query("*"))
        (span,) = exporter.get_finished_spans()
        assert span.name == "redis ft_search"
        exporter.clear()
        instrumentor.uninstrument()
        # Removing traceAI must leave independently installed command tracing on.
        if asynchronous:
            asyncio.run(clients[1].execute_command("PING"))
        else:
            clients[0].execute_command("PING")
        assert len(exporter.get_finished_spans()) == 1
    finally:
        command.uninstrument()


@contextmanager
def wire_exporter():
    payloads = []

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            assert self.path == "/v1/traces"
            payloads.append(self.rfile.read(int(self.headers["Content-Length"])))
            self.send_response(200)
            self.end_headers()

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    exporter = OTLPSpanExporter(
        endpoint=f"http://127.0.0.1:{server.server_port}/v1/traces",
        headers={"x-test": "synthetic"},
        timeout=3,
    )
    try:
        yield exporter, payloads
    finally:
        exporter.shutdown()
        server.shutdown()
        server.server_close()
        thread.join(timeout=3)


@pytest.mark.parametrize(
    "flag", ["FI_HIDE_ERROR_DETAILS", "FI_HIDE_INPUTS", "FI_HIDE_OUTPUTS"]
)
@pytest.mark.parametrize("asynchronous", [False, True])
def test_error_privacy_on_otlp_wire(
    telemetry, clients, monkeypatch, flag, asynchronous
):
    instrumentor, provider, _ = telemetry
    monkeypatch.setenv(flag, "true")
    error = ValueError(SECRET)

    def fail(self, *args, **kwargs):
        raise error

    async def afail(self, *args, **kwargs):
        await asyncio.sleep(0)
        raise error

    monkeypatch.setattr(Redis, "execute_command", fail)
    monkeypatch.setattr(AsyncRedis, "execute_command", afail)
    with wire_exporter() as (exporter, payloads):
        provider.add_span_processor(SimpleSpanProcessor(exporter))
        instrumentor.instrument(tracer_provider=provider)
        with pytest.raises(ValueError) as caught:
            result = clients[int(asynchronous)].ft("documents").search(Query(SECRET))
            if asynchronous:
                asyncio.run(result)
        assert caught.value is error
        assert len(payloads) == 1
        assert SECRET.encode() not in payloads[0]
        request = ExportTraceServiceRequest.FromString(payloads[0])
        (span,) = request.resource_spans[0].scope_spans[0].spans
        assert span.status.code == 2  # STATUS_CODE_ERROR
        assert not span.status.message
        assert span.name == "redis ft_search"
        attributes = {
            attribute.key: attribute.value.string_value for attribute in span.attributes
        }
        assert attributes["error.type"] == "ValueError"
        (event,) = span.events
        assert {a.key for a in event.attributes} == {
            "exception.type",
            "exception.escaped",
        }


@pytest.mark.parametrize("protocol", [2, 3, None])
@pytest.mark.parametrize("asynchronous", [False, True])
def test_success_shape_and_privacy_on_otlp_wire(
    telemetry, monkeypatch, protocol, asynchronous
):
    if protocol != 2 and not hasattr(Result, "from_resp3"):
        pytest.skip("These response modes are specific to current redis-py")
    instrumentor, provider, _ = telemetry
    response = (
        [9, b"doc:1", [b"content", SECRET.encode()]]
        if protocol == 2
        else {
            "total_results": 9,
            "results": [{"id": "doc:1", "extra_attributes": {"content": SECRET}}],
        }
    )

    def execute(self, *args, **kwargs):
        assert SECRET in str(args)
        return response

    async def aexecute(self, *args, **kwargs):
        return execute(self, *args, **kwargs)

    cls = AsyncRedis if asynchronous else Redis
    monkeypatch.setattr(cls, "execute_command", aexecute if asynchronous else execute)
    client = cls(protocol=protocol)
    with wire_exporter() as (exporter, payloads):
        provider.add_span_processor(SimpleSpanProcessor(exporter))
        instrumentor.instrument(tracer_provider=provider)
        result = client.ft("documents").search(Query(SECRET))
        if asynchronous:
            result = asyncio.run(result)
        if protocol == 3:
            # Explicit RESP3 + legacy responses returns the raw mapping.
            assert result == response
        else:
            assert result.docs[0].content == SECRET
        assert len(payloads) == 1
        assert SECRET.encode() not in payloads[0]
        request = ExportTraceServiceRequest.FromString(payloads[0])
        (span,) = request.resource_spans[0].scope_spans[0].spans
        attributes = {attribute.key: attribute.value for attribute in span.attributes}
        if protocol == 3:
            assert "db.vector.results.count" not in attributes
            assert "db.vector.results.total" not in attributes
        else:
            assert attributes["db.vector.results.count"].int_value == 1
            assert attributes["db.vector.results.total"].int_value == 9
        assert span.kind == 3  # SPAN_KIND_CLIENT
        assert span.status.code == 1  # STATUS_CODE_OK


def test_explicit_config_and_default_error_details(telemetry, clients, monkeypatch):
    instrumentor, provider, exporter = telemetry
    error = ValueError(SECRET)

    def fail(self, *args, **kwargs):
        raise error

    monkeypatch.setattr(Redis, "execute_command", fail)
    for hide in (True, False):
        instrumentor.instrument(
            tracer_provider=provider, config=TraceConfig(hide_error_details=hide)
        )
        with pytest.raises(ValueError) as caught:
            clients[0].ft("documents").search(Query("*"))
        assert caught.value is error
        (span,) = exporter.get_finished_spans()
        assert len(span.events) == 1
        assert (SECRET in (span.status.description or "")) is (not hide)
        instrumentor.uninstrument()
        exporter.clear()


def test_async_span_lifetime_and_context_isolation(telemetry, clients, monkeypatch):
    instrumentor, provider, exporter = telemetry

    async def run():
        entered = asyncio.Event()
        release = asyncio.Event()

        async def execute(self, *args, **kwargs):
            entered.set()
            await release.wait()
            return [0]

        monkeypatch.setattr(AsyncRedis, "execute_command", execute)
        instrumentor.instrument(tracer_provider=provider)
        task = asyncio.create_task(clients[1].ft("documents").search(Query("*")))
        await asyncio.wait_for(entered.wait(), timeout=3)
        assert (
            not exporter.get_finished_spans()
        )  # End after await, not coroutine creation.
        clients[0].ft("documents").search(Query("*"))
        assert len(exporter.get_finished_spans()) == 1  # Other tasks aren't suppressed.
        release.set()
        await task
        assert len(exporter.get_finished_spans()) == 2

    asyncio.run(run())


def test_async_cancellation_unwinds_context(telemetry, clients, monkeypatch):
    instrumentor, provider, exporter = telemetry

    async def run():
        entered = asyncio.Event()

        async def execute(self, *args, **kwargs):
            entered.set()
            await asyncio.Event().wait()

        monkeypatch.setattr(AsyncRedis, "execute_command", execute)
        instrumentor.instrument(tracer_provider=provider)
        task = asyncio.create_task(clients[1].ft("documents").search(Query("*")))
        await asyncio.wait_for(entered.wait(), timeout=3)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        (cancelled,) = exporter.get_finished_spans()
        assert cancelled.status.status_code is trace.StatusCode.ERROR
        assert len(cancelled.events) == 1
        clients[0].ft("documents").search(Query("*"))
        assert len(exporter.get_finished_spans()) == 2

    asyncio.run(run())
