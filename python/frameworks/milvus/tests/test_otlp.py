"""Export actual OTLP/HTTP protobuf to a loopback-only receiver."""

import socket
from http.server import BaseHTTPRequestHandler, HTTPServer
from threading import Thread

import pymilvus
import pytest
import requests
from fi_instrumentation import TraceConfig
from opentelemetry.exporter.otlp.proto.http import Compression
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
from opentelemetry.proto.collector.trace.v1.trace_service_pb2 import (
    ExportTraceServiceRequest,
)
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from traceai_milvus import MilvusInstrumentor


def test_otlp_http_contains_metadata_but_no_private_content(monkeypatch):
    connect = socket.socket.connect

    def loopback_only(sock, address):
        if not isinstance(address, tuple) or address[0] not in {"127.0.0.1", "::1"}:
            raise AssertionError("Only loopback connections are allowed")
        return connect(sock, address)

    monkeypatch.setattr(socket.socket, "connect", loopback_only)
    payloads = []

    class Receiver(BaseHTTPRequestHandler):
        def do_POST(self):
            assert self.path == "/v1/traces"
            assert self.headers["Content-Type"] == "application/x-protobuf"
            payloads.append(self.rfile.read(int(self.headers["Content-Length"])))
            self.send_response(200)
            self.end_headers()

        def log_message(self, *args):
            return None

    class Client:
        def search(self, collection_name, data=None, filter="", limit=10, **kwargs):
            if kwargs.get("fail"):
                raise ValueError("private-error-canary")
            return [[{"id": "private-id-canary", "text": "private-result-canary"}]]

    monkeypatch.setattr(pymilvus, "MilvusClient", Client)
    server = HTTPServer(("127.0.0.1", 0), Receiver)
    worker = Thread(target=server.serve_forever, daemon=True)
    worker.start()
    session = requests.Session()
    session.trust_env = False
    provider = TracerProvider()
    provider.add_span_processor(
        SimpleSpanProcessor(
            OTLPSpanExporter(
                endpoint=f"http://127.0.0.1:{server.server_port}/v1/traces",
                headers={"x-test": "synthetic"},
                compression=Compression.NoCompression,
                timeout=2,
                session=session,
            )
        )
    )
    instrumentor = MilvusInstrumentor()
    try:
        instrumentor.instrument(
            tracer_provider=provider, config=TraceConfig(hide_inputs=True)
        )
        with provider.get_tracer("test").start_as_current_span("parent") as parent:
            Client().search("documents", data=[[0.1]], filter="private-filter-canary")
            with pytest.raises(ValueError, match="private-error-canary"):
                Client().search("documents", filter="private-filter-canary", fail=True)
        assert provider.force_flush()
        assert len(payloads) == 3
        assert b"private-" not in b"".join(payloads)
        spans = [
            span
            for payload in payloads
            for resource in ExportTraceServiceRequest.FromString(payload).resource_spans
            for scope in resource.scope_spans
            for span in scope.spans
        ]
        search, failure, outer = spans
        assert search.name == failure.name == "milvus search"
        assert outer.name == "parent"
        assert search.kind == failure.kind == 3  # SPAN_KIND_CLIENT
        assert (
            int.from_bytes(search.parent_span_id, "big")
            == parent.get_span_context().span_id
        )
        assert search.trace_id == failure.trace_id == outer.trace_id
        assert search.status.code == 1  # STATUS_CODE_OK
        assert failure.status.code == 2  # STATUS_CODE_ERROR
        assert failure.status.message == ""
        assert len(failure.events) == 1
        assert {a.key for a in failure.events[0].attributes} == {
            "exception.type",
            "exception.escaped",
        }
    finally:
        if instrumentor.is_instrumented_by_opentelemetry:
            instrumentor.uninstrument()
        provider.shutdown()
        session.close()
        server.shutdown()
        server.server_close()
        worker.join(timeout=2)
