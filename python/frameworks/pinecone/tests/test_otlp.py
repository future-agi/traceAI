"""Send real operation spans through the OTLP/HTTP protobuf exporter locally."""

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
from opentelemetry.proto.collector.trace.v1.trace_service_pb2 import ExportTraceServiceRequest
from opentelemetry.sdk.trace.export import SpanExportResult

from .test_instrumentor import Index, endpoint  # noqa: F401


def test_otlp_http_payload(endpoint, instrument_pinecone, tracer_provider, in_memory_span_exporter):
    received = []

    class Receiver(BaseHTTPRequestHandler):
        def do_POST(self):
            assert self.path == "/v1/traces"
            assert self.headers["Content-Type"] == "application/x-protobuf"
            received.append(self.rfile.read(int(self.headers["Content-Length"])))
            self.send_response(200)
            self.end_headers()

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Receiver)
    worker = threading.Thread(target=server.serve_forever, daemon=True)
    worker.start()
    exporter = OTLPSpanExporter(endpoint=f"http://127.0.0.1:{server.server_port}/v1/traces", timeout=2)
    index = Index(api_key="SYNTHETIC_PINECONE_KEY", host=endpoint[0])
    try:
        with tracer_provider.get_tracer("test").start_as_current_span("retrieval"):
            result = index.upsert(vectors=[{"id": "vec1", "values": [0.1, 0.2, 0.3], "metadata": {"text": "PRIVATE_DOCUMENT_CONTENT"}}])
            assert result.upserted_count == 2
            index.query(vector=[0.1, 0.2, 0.3], top_k=2, include_metadata=True)
        assert exporter.export(in_memory_span_exporter.get_finished_spans()) == SpanExportResult.SUCCESS
        payload, = received
        assert b"SYNTHETIC_PINECONE_KEY" not in payload
        assert b"PRIVATE_DOCUMENT_CONTENT" not in payload
        assert b"doc1" not in payload  # The response's metadata is not captured either.
        wire = ExportTraceServiceRequest.FromString(payload)
        spans = [s for r in wire.resource_spans for scope in r.scope_spans for s in scope.spans]
        by_name = {s.name: s for s in spans}
        assert set(by_name) == {"pinecone upsert", "pinecone query", "retrieval"}
        for operation in ("pinecone upsert", "pinecone query"):
            child = by_name[operation]
            assert child.trace_id == by_name["retrieval"].trace_id
            assert child.parent_span_id == by_name["retrieval"].span_id
            assert child.kind == 3
            assert child.status.code == 1
            assert child.end_time_unix_nano >= child.start_time_unix_nano
        attributes = {a.key: a.value for a in by_name["pinecone query"].attributes}
        assert attributes["db.system"].string_value == "pinecone"
        assert attributes["db.vector.results.count"].int_value == 1
        assert json.loads(attributes["db.vector.results.ids"].string_value) == ["vec1"]
        assert "input.value" not in attributes
        assert "output.value" not in attributes
    finally:
        if hasattr(index, "close"):
            index.close()
        exporter.shutdown()
        server.shutdown()
        worker.join()
        server.server_close()
