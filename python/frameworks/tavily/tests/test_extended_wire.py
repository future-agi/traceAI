"""OTLP protobuf export and canonical OTLP JSON round-trip, plain and gzip."""

import asyncio
import gzip
import json
import threading
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest
import requests
from fi_instrumentation import TraceConfig
from google.protobuf.json_format import MessageToDict, MessageToJson
from opentelemetry.exporter.otlp.proto.common.trace_encoder import encode_spans
from opentelemetry.exporter.otlp.proto.http import Compression
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
from opentelemetry.proto.collector.trace.v1.trace_service_pb2 import ExportTraceServiceRequest
from opentelemetry.sdk.trace.export import SpanExportResult

from _support import instrumented
from _tavily_fake import TAVILY_KEY
from test_extended_operations import (
    METHODS, PRIVATE_OUTPUT, PRIVATE_URL, QUESTION, REQUEST_ID, Transport, argument,
)


@contextmanager
def receiver():
    records = []

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            body = self.rfile.read(int(self.headers["Content-Length"]))
            if self.headers.get("Content-Encoding") == "gzip":
                body = gzip.decompress(body)
            if self.headers["Content-Type"] == "application/json":
                payload = json.loads(body)
            else:
                message = ExportTraceServiceRequest.FromString(body)
                payload = MessageToDict(message)
            records.append((dict(self.headers), payload))
            self.send_response(200)
            self.send_header("Content-Length", "0")
            self.end_headers()

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}/v1/traces", records
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


@pytest.mark.parametrize("encoding", ["json", "protobuf"])
@pytest.mark.parametrize("compressed", [False, True])
@pytest.mark.parametrize("is_async", [False, True])
def test_extended_operations_are_private_on_otlp_wire(monkeypatch, encoding, compressed, is_async):
    transport = Transport()
    with instrumented(config=TraceConfig(hide_inputs=True)) as traced:
        async def run():
            client = await transport.async_client()
            try:
                for method in METHODS:
                    await getattr(client, method)(argument(method))
                stream = await client.research(QUESTION, stream=True)
                assert b"".join([chunk async for chunk in stream])
                transport.status = 400
                with pytest.raises(Exception):
                    await client.crawl(PRIVATE_URL)
            finally:
                await client.close()

        if is_async:
            asyncio.run(run())
        else:
            client = transport.sync_client(monkeypatch)
            for method in METHODS:
                getattr(client, method)(argument(method))
            assert b"".join(client.research(QUESTION, stream=True))
            transport.status = 400
            with pytest.raises(Exception):
                client.crawl(PRIVATE_URL)
        spans = traced.spans()
        assert len(spans) == 6
        with receiver() as (endpoint, records):
            if encoding == "protobuf":
                exporter = OTLPSpanExporter(
                    endpoint=endpoint,
                    compression=Compression.Gzip if compressed else Compression.NoCompression,
                )
                try:
                    assert exporter.export(spans) == SpanExportResult.SUCCESS
                finally:
                    exporter.shutdown()
            else:
                # Python's HTTP exporter sends protobuf. Transcode its canonical
                # encoder output to test the equivalent OTLP JSON representation.
                body = MessageToJson(encode_spans(spans)).encode()
                headers = {"Content-Type": "application/json"}
                if compressed:
                    body = gzip.compress(body)
                    headers["Content-Encoding"] = "gzip"
                response = requests.post(endpoint, data=body, headers=headers, timeout=5)
                assert response.status_code == 200
        assert len(records) == 1
        headers, payload = records[0]
        assert (headers.get("Content-Encoding") == "gzip") is compressed
        decoded = [
            span for resource in payload["resourceSpans"]
            for scope in resource["scopeSpans"] for span in scope["spans"]
        ]
        assert [span["name"] for span in decoded] == [span.name for span in spans]
        assert decoded[-1]["status"]["code"] == "STATUS_CODE_ERROR"
        assert len(decoded[-1]["events"]) == 1
        wire = json.dumps(payload)
        for private in (TAVILY_KEY, QUESTION, PRIVATE_OUTPUT, PRIVATE_URL, REQUEST_ID):
            assert private not in wire
