"""A local stand-in for an OpenAI-compatible API and the Future AGI collector, for zero-code tests."""

import gzip
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Dict, List

CHAT_RESPONSE = {
    "id": "chatcmpl-local",
    "object": "chat.completion",
    "created": 0,
    "model": "gpt-4o-mini",
    "choices": [
        {"index": 0, "message": {"role": "assistant", "content": "pong"}, "finish_reason": "stop"}
    ],
    "usage": {"prompt_tokens": 5, "completion_tokens": 1, "total_tokens": 6},
}


class MockServer:
    """Answers ``POST .../chat/completions`` and collects ``POST .../v1/traces`` exports."""

    def __init__(self) -> None:
        self.collected: List[bytes] = []
        self.requests: List[Dict[str, Any]] = []
        owner = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *args: Any) -> None:
                pass

            def _body(self) -> bytes:
                length = int(self.headers.get("Content-Length") or 0)
                body = self.rfile.read(length) if length else b""
                if (self.headers.get("Content-Encoding") or "").lower() == "gzip":
                    body = gzip.decompress(body)
                return body

            def _send(self, status: int, content_type: str, body: bytes) -> None:
                self.send_response(status)
                self.send_header("Content-Type", content_type)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_GET(self) -> None:
                self._send(200, "application/json", b'{"data": []}')

            def do_POST(self) -> None:
                body = self._body()
                owner.requests.append({"path": self.path, "headers": dict(self.headers), "body": body})
                if self.path.endswith("/v1/traces"):
                    owner.collected.append(body)
                    self._send(200, "application/x-protobuf", b"")
                elif self.path.endswith("/chat/completions"):
                    self._send(200, "application/json", json.dumps(CHAT_RESPONSE).encode())
                else:
                    self._send(404, "application/json", b'{"error": "not found"}')

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.server.server_address[1]}"

    def trace_requests(self) -> List[Dict[str, Any]]:
        return [r for r in self.requests if r["path"].endswith("/v1/traces")]

    def __enter__(self) -> "MockServer":
        self.thread.start()
        return self

    def __exit__(self, *exc: Any) -> None:
        self.server.shutdown()
        self.server.server_close()


def decode_spans(payloads: List[bytes]) -> List[Dict[str, Any]]:
    """Decode OTLP/HTTP protobuf export bodies into simple dicts."""
    from opentelemetry.proto.collector.trace.v1.trace_service_pb2 import ExportTraceServiceRequest

    spans = []
    for payload in payloads:
        request = ExportTraceServiceRequest()
        request.ParseFromString(payload)
        for resource_spans in request.resource_spans:
            resource = {a.key: _value(a.value) for a in resource_spans.resource.attributes}
            for scope_spans in resource_spans.scope_spans:
                for span in scope_spans.spans:
                    spans.append({
                        "name": span.name,
                        "resource": resource,
                        "attributes": {a.key: _value(a.value) for a in span.attributes},
                        "trace_id": span.trace_id.hex(),
                        "span_id": span.span_id.hex(),
                        "parent_span_id": span.parent_span_id.hex(),
                    })
    return spans


def _value(value: Any) -> Any:
    kind = value.WhichOneof("value")
    if kind == "array_value":
        return [_value(v) for v in value.array_value.values]
    return getattr(value, kind) if kind else None
