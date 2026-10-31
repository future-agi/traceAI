"""Real PyMongo clients against a synthetic loopback wire server and OTLP sink."""

import asyncio
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import Thread

from mockupdb import MockupDB, go
import pymongo
import pytest
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
from opentelemetry.proto.collector.trace.v1.trace_service_pb2 import ExportTraceServiceRequest
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from opentelemetry.trace import StatusCode

from traceai_mongodb import MongoDBInstrumentor


@pytest.fixture
def driver():
    server = MockupDB(auto_ismaster=True, max_wire_version=25)
    server.run()
    server.autoresponds(
        lambda request: request.ok() if request.command_name in ("endSessions", "killCursors") else None
    )
    provider = TracerProvider()
    exporter = InMemorySpanExporter()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    instrumentor = MongoDBInstrumentor()
    instrumentor.instrument(tracer_provider=provider)
    try:
        yield server, provider, exporter
    finally:
        instrumentor.uninstrument()
        provider.shutdown()
        server.stop()


def test_insert_many_generator_reaches_driver_unchanged(driver):
    server, _, exporter = driver
    with pymongo.MongoClient(server.uri, serverSelectionTimeoutMS=3000) as client:
        documents = ({"_id": i, "private": "SECRET-DOCUMENT"} for i in range(2))
        result = go(client.testdb.rows.insert_many, documents)
        request = server.receives(timeout=5)
        assert request.command_name == "insert"
        assert [doc["_id"] for doc in request.doc["documents"]] == [0, 1]
        assert exporter.get_finished_spans() == ()
        request.ok(n=2)
        assert result(timeout=5).inserted_ids == [0, 1]
        spans = exporter.get_finished_spans()
        assert len(spans) == 1
        assert spans[0].attributes["db.vector.upsert.count"] == 2
        assert "SECRET-DOCUMENT" not in str(dict(spans[0].attributes))


@pytest.mark.parametrize("asynchronous", [False, True])
def test_real_cursor_getmore_error_and_parent(driver, asynchronous):
    if asynchronous and not hasattr(pymongo, "AsyncMongoClient"):
        pytest.skip("AsyncMongoClient is not available at the PyMongo 4.6 floor")
    server, provider, exporter = driver

    async def async_query():
        async with pymongo.AsyncMongoClient(server.uri, serverSelectionTimeoutMS=3000) as client:
            with provider.get_tracer("app").start_as_current_span("parent"):
                cursor = client.testdb.rows.find({"private": "SECRET-FILTER"}, batch_size=1)
                assert exporter.get_finished_spans() == ()
                with pytest.raises(pymongo.errors.OperationFailure):
                    await cursor.to_list()

    def sync_query():
        with pymongo.MongoClient(server.uri, serverSelectionTimeoutMS=3000) as client:
            with provider.get_tracer("app").start_as_current_span("parent"):
                cursor = client.testdb.rows.find({"private": "SECRET-FILTER"}, batch_size=1)
                assert exporter.get_finished_spans() == ()
                with pytest.raises(pymongo.errors.OperationFailure):
                    list(cursor)

    result = go(lambda: asyncio.run(async_query())) if asynchronous else go(sync_query)
    query = server.receives(timeout=5)
    assert query.command_name == "find"
    assert exporter.get_finished_spans() == ()
    query.ok(cursor={"id": 42, "ns": "testdb.rows", "firstBatch": [{"_id": 1}]})
    getmore = server.receives(timeout=5)
    assert getmore.command_name == "getMore"
    getmore.command_err(code=123, errmsg="SECRET-SERVER-ERROR")
    result(timeout=5)
    spans = exporter.get_finished_spans()
    commands = [span for span in spans if span.name in ("testdb.find", "testdb.getMore")]
    parent = next(span for span in spans if span.name == "parent")
    assert len(commands) == 2
    assert commands[1].status.status_code is StatusCode.ERROR
    for span in commands:
        assert span.parent.span_id == parent.context.span_id
        assert span.context.trace_id == parent.context.trace_id
        assert "SECRET" not in str(dict(span.attributes)) + str(span.status.description)


def test_otlp_wire_preserves_vector_metadata_without_payloads(driver):
    server, provider, _ = driver
    bodies = []

    class Sink(BaseHTTPRequestHandler):
        def do_POST(self):
            assert self.path == "/v1/traces"
            bodies.append(self.rfile.read(int(self.headers["Content-Length"])))
            self.send_response(200)
            self.send_header("Content-Type", "application/x-protobuf")
            self.end_headers()

        def log_message(self, *args):
            pass

    receiver = ThreadingHTTPServer(("127.0.0.1", 0), Sink)
    thread = Thread(target=receiver.serve_forever, daemon=True)
    thread.start()
    provider.add_span_processor(SimpleSpanProcessor(OTLPSpanExporter(
        endpoint=f"http://127.0.0.1:{receiver.server_port}/v1/traces",
        headers={}, timeout=5,
    )))
    try:
        with pymongo.MongoClient(server.uri, serverSelectionTimeoutMS=3000) as client:
            def query():
                with provider.get_tracer("app").start_as_current_span("vector-parent"):
                    return list(client.testdb.rows.aggregate([{"$vectorSearch": {
                        "index": "vectors", "path": "embedding", "queryVector": [0.123456, 0.654321],
                        "numCandidates": 20, "limit": 3, "filter": {"private": "SECRET-FILTER"},
                    }}]))

            result = go(query)
            request = server.receives(timeout=5)
            assert request.command_name == "aggregate"
            request.ok(cursor={"id": 0, "ns": "testdb.rows", "firstBatch": [{"private": "SECRET-RESULT"}]})
            assert result(timeout=5) == [{"private": "SECRET-RESULT"}]
            error_result = go(client.testdb.command, {"find": "rows", "filter": {"private": "SECRET"}})
            error_request = server.receives(timeout=5)
            error_request.command_err(code=123, errmsg="SECRET-ERROR", codeName="SECRET-CODE-NAME")
            with pytest.raises(pymongo.errors.OperationFailure):
                error_result(timeout=5)
        provider.force_flush()
        decoded = []
        for body in bodies:
            assert b"SECRET" not in body
            assert b"queryVector" not in body
            message = ExportTraceServiceRequest.FromString(body)
            for resource in message.resource_spans:
                for scope in resource.scope_spans:
                    decoded.extend(scope.spans)
        command = next(span for span in decoded if span.name == "testdb.aggregate")
        parent = next(span for span in decoded if span.name == "vector-parent")
        assert command.trace_id == parent.trace_id
        assert command.parent_span_id == parent.span_id
        assert command.end_time_unix_nano >= command.start_time_unix_nano
        attributes = {attr.key: attr.value for attr in command.attributes}
        assert attributes["db.vector.query.vector_dimensions"].int_value == 2
        assert attributes["db.vector.query.top_k"].int_value == 3
        assert attributes["db.vector.results.count"].int_value == 1
        error = next(span for span in decoded if span.name == "testdb.find")
        assert error.status.code == 2  # OTLP STATUS_CODE_ERROR
        assert error.status.message == "MongoDB command failed"
    finally:
        receiver.shutdown()
        receiver.server_close()
        thread.join(timeout=5)
