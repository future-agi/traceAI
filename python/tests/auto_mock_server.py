"""A local stand-in for AI provider APIs and the Future AGI collector, for auto-instrumentation tests."""

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Dict, List

CHAT_RESPONSE = {
    "id": "chatcmpl-local",
    "object": "chat.completion",
    "model": "gpt-4o-mini",
    "choices": [{"index": 0, "message": {"role": "assistant", "content": "pong"}, "finish_reason": "stop"}],
    "usage": {"prompt_tokens": 5, "completion_tokens": 1, "total_tokens": 6},
}
STREAM_CHUNKS = [
    {"id": "chatcmpl-s", "model": "gpt-4o-mini", "choices": [{"index": 0, "delta": {"role": "assistant", "content": "po"}}]},
    {"id": "chatcmpl-s", "model": "gpt-4o-mini", "choices": [{"index": 0, "delta": {"content": "ng"}, "finish_reason": "stop"}]},
    {"id": "chatcmpl-s", "model": "gpt-4o-mini", "choices": [], "usage": {"prompt_tokens": 5, "completion_tokens": 2, "total_tokens": 7}},
]
ANTHROPIC_RESPONSE = {
    "id": "msg_local", "type": "message", "role": "assistant", "model": "claude-haiku-4-5",
    "content": [{"type": "text", "text": "hi"}], "stop_reason": "end_turn",
    "usage": {"input_tokens": 3, "output_tokens": 1},
}
GEMINI_RESPONSE = {
    "candidates": [{"content": {"role": "model", "parts": [{"text": "bonjour"}]}, "finishReason": "STOP"}],
    "usageMetadata": {"promptTokenCount": 2, "candidatesTokenCount": 1, "totalTokenCount": 3},
    "modelVersion": "gemini-2.5-flash",
}
AUDIO_BYTES = b"ID3" + b"\0" * 4093


def sse_body() -> bytes:
    return b"".join(b"data: " + json.dumps(c).encode() + b"\n\n" for c in STREAM_CHUNKS) + b"data: [DONE]\n\n"


class MockServer:
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
                return self.rfile.read(length) if length else b""

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
                    try:
                        payload = json.loads(body or b"{}")
                    except ValueError:
                        self._send(400, "application/json", b'{"error": {"message": "invalid json"}}')
                        return
                    if payload.get("model") == "broken":
                        self._send(401, "application/json", b'{"error": {"message": "bad key"}}')
                    elif payload.get("stream"):
                        self.send_response(200)
                        self.send_header("Content-Type", "text/event-stream")
                        self.send_header("Transfer-Encoding", "chunked")
                        self.end_headers()
                        for piece in sse_body().split(b"\n\n"):
                            if piece:
                                data = piece + b"\n\n"
                                self.wfile.write(f"{len(data):x}\r\n".encode() + data + b"\r\n")
                                self.wfile.flush()
                        self.wfile.write(b"0\r\n\r\n")
                    else:
                        self._send(200, "application/json", json.dumps(CHAT_RESPONSE).encode())
                elif self.path.endswith("/v1/messages"):
                    self._send(200, "application/json", json.dumps(ANTHROPIC_RESPONSE).encode())
                elif self.path.endswith("/audio/speech"):
                    self._send(200, "audio/mpeg", AUDIO_BYTES)
                elif self.path.endswith("/audio/transcriptions"):
                    self._send(200, "application/json", b'{"text": "hello from audio"}')
                elif ":generateContent" in self.path:
                    self._send(200, "application/json", json.dumps(GEMINI_RESPONSE).encode())
                else:
                    self._send(404, "application/json", b'{"error": "not found"}')

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.server.server_address[1]}"

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
                        "parent_span_id": span.parent_span_id.hex(),
                        "span_id": span.span_id.hex(),
                    })
    return spans


def _value(value: Any) -> Any:
    kind = value.WhichOneof("value")
    if kind == "array_value":
        return [_value(v) for v in value.array_value.values]
    return getattr(value, kind) if kind else None
