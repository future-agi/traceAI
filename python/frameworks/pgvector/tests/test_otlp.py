"""Serialize actual OTLP HTTP exports to a localhost-only receiver."""

import asyncio
import json
from http.server import BaseHTTPRequestHandler, HTTPServer
from threading import Thread

import psycopg
import psycopg2
from google.protobuf.json_format import MessageToDict
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
from opentelemetry.proto.collector.trace.v1.trace_service_pb2 import (
    ExportTraceServiceRequest,
)
from opentelemetry.sdk.trace.export import SimpleSpanProcessor

from .test_instrumentor import AsyncCursor3, Cursor2, SyntheticDriverError, telemetry  # noqa: F401


def test_otlp_wire_privacy_and_shape(telemetry):  # noqa: F811 - imported pytest fixture
    provider, _, instrumentor = telemetry
    requests = []

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            body = self.rfile.read(int(self.headers["Content-Length"]))
            request = ExportTraceServiceRequest()
            request.ParseFromString(body)
            requests.append(request)
            self.send_response(200)
            self.end_headers()

        def log_message(self, *args):
            pass

    server = HTTPServer(("127.0.0.1", 0), Handler)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    exporter = OTLPSpanExporter(
        endpoint=f"http://127.0.0.1:{server.server_port}/v1/traces"
    )
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    try:
        cursor = psycopg2.connect(cursor_factory=Cursor2).cursor()
        with provider.get_tracer("wire-test").start_as_current_span("parent"):
            cursor.execute(
                query="SELECT secret_embedding <=> '[0.8675309,0.1234567]' FROM secret_table LIMIT 3"
            )
            cursor.executemany(
                "INSERT INTO secret_table VALUES (%s)", iter([("secret-param",)])
            )

            async def fail():
                connection = await psycopg.AsyncConnection.connect(
                    cursor_factory=AsyncCursor3
                )
                try:
                    await connection.cursor().execute(
                        "SELECT secret_embedding <-> %s", "FAIL"
                    )
                except SyntheticDriverError:
                    pass
                else:
                    raise AssertionError("The driver's exception must propagate")

            asyncio.run(fail())
        provider.force_flush()
        wire = json.dumps([MessageToDict(request) for request in requests])
        for forbidden in (
            "secret",
            "8675309",
            "1234567",
            "db.statement",
            "db.query.text",
            "exception.message",
            "exception.stacktrace",
        ):
            assert forbidden not in wire
        spans = [
            s
            for r in requests
            for resource in r.resource_spans
            for scope in resource.scope_spans
            for s in scope.spans
        ]
        assert len(spans) == 4
        database = [s for s in spans if s.name != "parent"]
        (parent,) = [s for s in spans if s.name == "parent"]
        assert all(s.kind == 3 for s in database)  # SPAN_KIND_CLIENT
        assert all(s.parent_span_id == parent.span_id for s in database)
        assert all(s.trace_id == parent.trace_id for s in database)
        assert database[-1].status.code == 2  # STATUS_CODE_ERROR
        assert not database[-1].status.message
        assert not database[-1].events
        attributes = {a.key: a.value for a in database[0].attributes}
        assert attributes["db.vector.distance_type"].string_value == "cosine_distance"
        assert attributes["db.vector.query.top_k"].int_value == 3
    finally:
        instrumentor.uninstrument()
        provider.force_flush()
        server.shutdown()
        thread.join(timeout=5)
        server.server_close()
