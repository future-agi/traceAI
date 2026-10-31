"""Offline driver-event contracts; no database or credentials required."""

from datetime import timedelta
import socket

import pytest
from opentelemetry.instrumentation.pymongo import PymongoInstrumentor
from opentelemetry.instrumentation.utils import suppress_instrumentation
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from opentelemetry.trace import StatusCode
from pymongo import MongoClient, monitoring
from pymongo.collection import Collection

from traceai_mongodb import MongoDBInstrumentor


@pytest.fixture
def tracing(monkeypatch):
    def forbid_network(*args, **kwargs):
        raise AssertionError("This test must not open a connection")

    monkeypatch.setattr(socket.socket, "connect", forbid_network)
    provider = TracerProvider()
    exporter = InMemorySpanExporter()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    instrumentor = MongoDBInstrumentor()
    instrumentor.instrument(tracer_provider=provider)
    yield instrumentor, provider, exporter
    instrumentor.uninstrument()
    provider.shutdown()


def started(command, request_id=1, connection=("localhost", 27017)):
    return monitoring.CommandStartedEvent(command, "testdb", request_id, connection, request_id)


def succeeded(command_name, reply=None, request_id=1, connection=("localhost", 27017)):
    return monitoring.CommandSucceededEvent(
        timedelta(milliseconds=3), reply or {"ok": 1}, command_name,
        request_id, connection, request_id,
    )


def failed(command_name, request_id=1, connection=("localhost", 27017)):
    return monitoring.CommandFailedEvent(
        timedelta(milliseconds=3), {"errmsg": "PRIVATE error document", "code": 123},
        command_name, request_id, connection, request_id,
    )


def listener():
    return PymongoInstrumentor()._commandtracer_instance


def test_lazy_find_and_no_method_patching(tracing):
    _, _, exporter = tracing
    with MongoClient("mongodb://127.0.0.1:1", connect=False) as client:
        cursor = client.testdb.rows.find({"private": "value"})
        assert exporter.get_finished_spans() == ()
        assert not hasattr(Collection.find, "__wrapped__")
        cursor.close()


def test_sync_client_registers_listener(tracing):
    with MongoClient("mongodb://127.0.0.1:1", connect=False) as client:
        assert listener() in client._event_listeners.event_listeners()


def test_async_client_registers_listener_without_eager_query(tracing):
    import asyncio
    import pymongo

    if not hasattr(pymongo, "AsyncMongoClient"):
        pytest.skip("AsyncMongoClient is not available at the PyMongo 4.6 floor")

    async def check():
        client = pymongo.AsyncMongoClient("mongodb://127.0.0.1:1", connect=False)
        try:
            assert listener() in client._event_listeners.event_listeners()
            cursor = client.testdb.rows.find({"private": "value"})
            assert tracing[2].get_finished_spans() == ()
            await cursor.close()
        finally:
            await client.close()

    asyncio.run(check())


def test_command_lifetime_parentage_getmore_and_failure(tracing):
    _, provider, exporter = tracing
    with provider.get_tracer("application").start_as_current_span("parent") as parent:
        listener().started(started({"find": "rows", "filter": {"private": "value"}}))
        assert exporter.get_finished_spans() == ()
        listener().succeeded(succeeded("find", {"cursor": {"firstBatch": [{"private": "value"}]}}))
        listener().started(started({"getMore": 42, "collection": "rows"}, 2))
        listener().failed(failed("getMore", 2))
        spans = exporter.get_finished_spans()
        assert [s.name for s in spans] == ["testdb.find", "testdb.getMore"]
        for span in spans:
            assert span.parent.span_id == parent.get_span_context().span_id
            assert span.context.trace_id == parent.get_span_context().trace_id
        assert spans[0].attributes["db.vector.results.count"] == 1
        assert spans[1].attributes["db.vector.collection.name"] == "rows"
        assert spans[1].status.status_code is StatusCode.ERROR
        assert "PRIVATE" not in str(spans[1].status.description)
        assert spans[1].attributes["db.response.status_code"] == 123


def test_vector_enrichment_does_not_capture_content(tracing):
    listener().started(started({"aggregate": "rows", "pipeline": [{"$vectorSearch": {
        "index": "vectors", "path": "embedding", "limit": 3, "numCandidates": 20,
        "queryVector": [0.123456, 0.654321], "filter": {"private": "secret"},
    }}]}))
    listener().succeeded(succeeded("aggregate", {"cursor": {"firstBatch": [{"private": "secret"}]}}))
    span = tracing[2].get_finished_spans()[0]
    assert span.attributes["db.vector.query.type"] == "vector_search"
    assert span.attributes["db.vector.index.name"] == "vectors"
    assert span.attributes["db.vector.query.field"] == "embedding"
    assert span.attributes["db.vector.query.top_k"] == 3
    assert span.attributes["db.vector.query.num_candidates"] == 20
    assert span.attributes["db.vector.query.vector_dimensions"] == 2
    assert span.attributes["db.mongodb.pipeline_stages"] == 1
    assert span.attributes["db.vector.results.count"] == 1
    for private in ("secret", "private", "0.123456", "queryVector"):
        assert private not in str(dict(span.attributes))


@pytest.mark.parametrize("command,reply,expected", [
    ({"insert": "rows", "documents": [{"x": 1}, {"x": 2}]}, {"n": 2}, {"db.vector.upsert.count": 2}),
    ({"update": "rows", "updates": []}, {"n": 4, "nModified": 3},
     {"db.vector.update.matched_count": 4, "db.vector.update.modified_count": 3}),
    ({"delete": "rows", "deletes": []}, {"n": 4}, {"db.vector.delete.count": 4}),
])
def test_write_counts_use_server_response(tracing, command, reply, expected):
    listener().started(started(command))
    listener().succeeded(succeeded(next(iter(command)), reply))
    span = tracing[2].get_finished_spans()[0]
    for key, value in expected.items():
        assert span.attributes[key] == value


@pytest.mark.parametrize("error_key", ["writeErrors", "writeConcernError"])
def test_write_error_is_not_success_and_does_not_leak(tracing, error_key):
    listener().started(started({"insert": "rows", "documents": [{"private": "secret"}]}))
    error = {"code": 11000, "errmsg": "PRIVATE duplicate data"}
    reply = {"ok": 1, error_key: [error] if error_key == "writeErrors" else error}
    listener().succeeded(succeeded("insert", reply))
    span = tracing[2].get_finished_spans()[0]
    assert span.status.status_code is StatusCode.ERROR
    assert "PRIVATE" not in str(span.status.description)


def test_overlapping_connections_and_duplicate_completion(tracing):
    listener().started(started({"find": "rows"}, connection=("localhost", 1)))
    listener().started(started({"find": "rows"}, connection=("localhost", 2)))
    listener().succeeded(succeeded("find", connection=("localhost", 2)))
    listener().failed(failed("find", connection=("localhost", 1)))
    listener().succeeded(succeeded("find", connection=("localhost", 2)))
    spans = tracing[2].get_finished_spans()
    assert len(spans) == 2
    assert spans[0].status.status_code is not StatusCode.ERROR
    assert spans[1].status.status_code is StatusCode.ERROR
    assert not listener()._span_dict


def test_native_enabled_after_adapter_does_not_add_second_listener(tracing):
    native = PymongoInstrumentor()
    original = listener()
    native.instrument(tracer_provider=tracing[1])
    assert listener() is original
    listener().started(started({"find": "rows"}))
    listener().succeeded(succeeded("find"))
    assert len(tracing[2].get_finished_spans()) == 1


def test_empty_batch_and_upsert_counts(tracing):
    listener().started(started({"find": "rows"}))
    listener().succeeded(succeeded("find", {"cursor": {"firstBatch": []}}))
    listener().started(started({"update": "rows"}, 2))
    listener().succeeded(succeeded("update", {"n": 2, "nModified": 1, "upserted": [{"_id": "PRIVATE"}]}, 2))
    spans = tracing[2].get_finished_spans()
    assert spans[0].attributes["db.vector.results.count"] == 0
    assert spans[1].attributes["db.vector.update.matched_count"] == 1
    assert "PRIVATE" not in str(dict(spans[1].attributes))


def test_malformed_vector_metadata_does_not_break_command(tracing):
    listener().started(started({"aggregate": "rows", "pipeline": [
        None, {"$vectorSearch": {"index": None, "path": {}, "limit": True, "queryVector": None}},
    ]}))
    listener().succeeded(succeeded("aggregate"))
    span = tracing[2].get_finished_spans()[0]
    assert span.status.status_code is not StatusCode.ERROR
    assert "db.vector.query.top_k" not in span.attributes
    assert "db.vector.query.vector_dimensions" not in span.attributes


def test_suppression_and_disable(tracing):
    instrumentor, _, exporter = tracing
    with suppress_instrumentation():
        listener().started(started({"find": "rows"}))
        listener().succeeded(succeeded("find"))
    assert exporter.get_finished_spans() == ()
    instrumentor.uninstrument()
    listener().started(started({"find": "rows"}))
    listener().succeeded(succeeded("find"))
    assert exporter.get_finished_spans() == ()
    instrumentor.instrument(tracer_provider=tracing[1])


def test_reenable_uses_new_provider_on_existing_client(tracing):
    instrumentor, _, first_exporter = tracing
    second_provider = TracerProvider()
    second_exporter = InMemorySpanExporter()
    second_provider.add_span_processor(SimpleSpanProcessor(second_exporter))
    with MongoClient("mongodb://127.0.0.1:1", connect=False) as client:
        original = listener()
        instrumentor.uninstrument()
        instrumentor.instrument(tracer_provider=second_provider)
        instrumentor.instrument(tracer_provider=second_provider)
        assert listener() is original
        assert client._event_listeners.event_listeners().count(original) == 1
        listener().started(started({"find": "rows"}))
        listener().succeeded(succeeded("find"))
        assert len(second_exporter.get_finished_spans()) == 1
        assert first_exporter.get_finished_spans() == ()
    second_provider.shutdown()


def test_disable_finishes_inflight_span(tracing):
    listener().started(started({"find": "rows"}))
    tracing[0].uninstrument()
    spans = tracing[2].get_finished_spans()
    assert len(spans) == 1
    assert spans[0].status.status_code is StatusCode.ERROR
    assert not listener()._span_dict
    tracing[0].instrument(tracer_provider=tracing[1])


def test_external_native_instrumentation_is_not_duplicated_or_disabled(tracing):
    instrumentor, provider, exporter = tracing
    instrumentor.uninstrument()
    native = PymongoInstrumentor()
    native.instrument(tracer_provider=provider)
    try:
        instrumentor.instrument(tracer_provider=provider)
        instrumentor.uninstrument()
        assert native.is_instrumented_by_opentelemetry
        listener().started(started({"find": "rows"}))
        listener().succeeded(succeeded("find"))
        assert len(exporter.get_finished_spans()) == 1
    finally:
        native.uninstrument()
        instrumentor.instrument(tracer_provider=provider)
