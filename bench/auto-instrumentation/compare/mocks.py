"""Additional deterministic provider fixtures and native Braintrust log receiver.

Only imports the existing stdlib mock/oracle helpers; never imports either agent.
"""
from __future__ import annotations

import base64
import gzip
import io
import json
import socket
import struct
import time
import wave
from http.server import BaseHTTPRequestHandler
from urllib.parse import urlparse

from mock_servers import LocalMocks, decode_otlp, fields, payload_for

INPUT = "compare-input-canary"
TEXT = "reply/compare-input-canary/café/終"
SECRET = "offline-provider-key-COMPARE-7813"
FI_KEY = "offline-fi-key-COMPARE-9134"
FI_SECRET = "offline-fi-secret-COMPARE-1823"
BT_KEY = "offline-braintrust-key-COMPARE-6715"
# A separated, nonsemantic first item makes names alone insufficient TTFT evidence.
STREAM_TEXT_DELAY_MS = 30
TOOL = {"id": "call_compare", "type": "function", "function": {
    "name": "lookup_weather", "arguments": '{"city":"Paris"}'}}


def audio_bytes():
    out = io.BytesIO()
    with wave.open(out, "wb") as wav:
        wav.setnchannels(1); wav.setsampwidth(2); wav.setframerate(8000)
        wav.writeframes((b"COMPARE_RAW_MEDIA_7831" + b"\0" * 1600)[:1600])
    return out.getvalue()


AUDIO = audio_bytes()
MEDIA_B64 = base64.b64encode(AUDIO).decode()


def encode(value):
    return json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode()


class EntityHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def setup(self):
        super().setup()
        self.connection.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        self.connection.settimeout(15)

    def log_message(self, *_):
        pass

    def date_time_string(self, timestamp=None):
        # SDK response metadata includes Date: deterministic across tool processes.
        return "Thu, 01 Jan 1970 00:00:00 GMT"

    def read_body(self):
        if self.headers.get("Transfer-Encoding", "").lower() == "chunked":
            parts, total = [], 0
            while True:
                size = int(self.rfile.readline(4096).split(b";", 1)[0].strip(), 16)
                if not size:
                    while self.rfile.readline(4096) not in (b"\r\n", b"\n", b""):
                        pass
                    body = b"".join(parts); break
                total += size
                if total > 16 * 1024 * 1024:
                    raise ValueError("entity too large")
                part = self.rfile.read(size)
                if len(part) != size or self.rfile.read(2) != b"\r\n":
                    raise ValueError("invalid chunk")
                parts.append(part)
        else:
            size = int(self.headers.get("Content-Length", 0))
            if not 0 <= size <= 16 * 1024 * 1024:
                raise ValueError("entity too large")
            body = self.rfile.read(size)
        if self.headers.get("Content-Encoding", "").lower() == "gzip":
            body = gzip.decompress(body)
        return body

    def reply(self, value, status=200, content_type="application/json"):
        body = value if isinstance(value, bytes) else encode(value)
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers(); self.wfile.write(body); self.wfile.flush()

    def stream(self, frames, delay, semantic_index):
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Content-Length", str(sum(map(len, frames))))
        self.end_headers()
        for index, frame in enumerate(frames):
            time.sleep(STREAM_TEXT_DELAY_MS / 1000 if index == semantic_index else delay)
            self.wfile.write(frame); self.wfile.flush()


def otlp_with_times(body, content_type):
    spans = decode_otlp(body, content_type)
    times = {}
    if "json" in content_type or body.lstrip().startswith(b"{"):
        root = json.loads(body)
        for resource in root.get("resourceSpans", root.get("resource_spans", [])):
            for scope in resource.get("scopeSpans", resource.get("scope_spans", [])):
                for span in scope.get("spans", []):
                    times[span.get("spanId", span.get("span_id"))] = (
                        span.get("startTimeUnixNano", span.get("start_time_unix_nano", 0)),
                        span.get("endTimeUnixNano", span.get("end_time_unix_nano", 0)))
    else:
        for resource in fields(body).get(1, []):
            for scope in fields(resource).get(2, []):
                for value in fields(scope).get(2, []):
                    f = fields(value)
                    times[f.get(2, [b""])[0].hex()] = tuple(
                        struct.unpack("<Q", f[k][0])[0] if k in f else 0 for k in (7, 8))
    for span in spans:
        span["start_ns"], span["end_ns"] = map(int, times.get(span["span_id"], (0, 0)))
    return spans


class CompareMocks(LocalMocks):
    """Reuse LocalMocks' loopback servers, joined threads and lifecycle."""

    def __init__(self, delay_ms=1):
        super().__init__(delay_ms)
        self.exports = []
        self.bt_rows = []
        owner = self

        class Provider(EntityHandler):
            def do_GET(self):
                self.reply({"ok": True})

            def do_POST(self):
                body = self.read_body()
                try:
                    value = json.loads(body)
                except (ValueError, UnicodeError):
                    value = {}
                path = urlparse(self.path).path
                with owner.lock:
                    owner.requests.append({"path": path, "model": value.get("model"),
                                           "stream": value.get("stream", False)})
                if path.endswith("/audio/transcriptions"):
                    self.reply({"task": "transcribe", "language": "english", "duration": 0.1,
                                "text": TEXT, "segments": [],
                                "usage": {"type": "duration", "seconds": 0.1}})
                elif path.endswith("/audio/speech"):
                    self.reply(AUDIO, content_type="audio/wav")
                elif path.endswith("/embeddings"):
                    self.reply({"object": "list", "model": "text-embedding-3-small",
                                "data": [{"object": "embedding", "index": 0, "embedding": [0.1, 0.2, 0.3]}],
                                "usage": {"prompt_tokens": 11, "total_tokens": 11}})
                elif ":generateContent" in path:
                    self.reply({"candidates": [{"content": {"role": "model", "parts": [{"text": TEXT}]},
                                                  "finishReason": "STOP", "index": 0}],
                                "usageMetadata": {"promptTokenCount": 11, "candidatesTokenCount": 7,
                                                  "totalTokenCount": 18},
                                "modelVersion": "gemini-2.0-flash", "responseId": "gemini-compare"})
                elif path.endswith("/messages"):
                    message = {"id": "msg_compare", "type": "message", "role": "assistant",
                               "model": "claude-sonnet-4-5", "content": [], "stop_reason": None,
                               "stop_sequence": None, "usage": {"input_tokens": 11, "output_tokens": 0}}
                    events = [("message_start", {"message": message}),
                              ("content_block_start", {"index": 0, "content_block": {"type": "text", "text": ""}}),
                              ("content_block_delta", {"index": 0, "delta": {"type": "text_delta", "text": TEXT}}),
                              ("content_block_stop", {"index": 0}),
                              ("message_delta", {"delta": {"stop_reason": "end_turn", "stop_sequence": None},
                                                  "usage": {"output_tokens": 7}}), ("message_stop", {})]
                    self.stream([b"event: " + kind.encode() + b"\ndata: " + encode({"type": kind, **v}) + b"\n\n"
                                 for kind, v in events], delay_ms / 1000, semantic_index=2)
                elif path.endswith("/chat/completions"):
                    if value.get("model") == "compare-error":
                        self.reply({"error": {"message": "offline deliberate error", "type": "invalid_request_error",
                                               "code": "compare_error"}}, status=400)
                    elif value.get("tools"):
                        self.reply({"id": "chatcmpl-tool", "object": "chat.completion", "created": 0,
                                    "model": "gpt-4o-mini", "choices": [{"index": 0, "message": {
                                        "role": "assistant", "content": None, "tool_calls": [TOOL]},
                                        "finish_reason": "tool_calls"}],
                                    "usage": {"prompt_tokens": 11, "completion_tokens": 7, "total_tokens": 18}})
                    else:
                        frames = payload_for(INPUT, bool(value.get("stream")))
                        if value.get("stream"):
                            self.stream(frames, delay_ms / 1000, semantic_index=2)
                        else:
                            self.reply(b"".join(frames))
                else:
                    self.reply({"error": "unexpected provider path", "path": path}, status=404)

        class Collector(EntityHandler):
            def handle_request(self):
                try:
                    body = self.read_body()
                    path = urlparse(self.path).path
                    ctype = self.headers.get("Content-Type", "")
                    if "apikey/login" in path:
                        self.reply({"org_info": [{"id": "00000000-0000-0000-0000-000000000001", "name": "offline",
                                                   "api_url": owner.collector_url, "proxy_url": owner.collector_url}]})
                        return
                    if "project" in path:
                        project = {"id": "00000000-0000-0000-0000-000000000002", "name": "offline-compare",
                                   "org_id": "00000000-0000-0000-0000-000000000001"}
                        self.reply({"project": project, "objects": [project], **project}); return
                    if path in ("/info", "/api/info", "/version"):
                        self.reply({"version": "offline-mock", "logs3_payload_max_bytes": 16000000}); return
                    if path == "/attachment":
                        self.reply({"signedUrl": owner.collector_url + "/attachment-upload", "headers": {}}); return
                    if path == "/attachment/status":
                        self.reply({}); return
                    if path == "/attachment-upload":
                        with owner.lock:
                            owner.exports.append({"path": path, "format": "attachment", "received_ns": time.monotonic_ns(),
                                                  "body_b64": base64.b64encode(body).decode()})
                        self.reply({}); return
                    if "logs" in path:
                        value = json.loads(body)
                        rows = value.get("rows", []) if isinstance(value, dict) else value
                        if not isinstance(rows, list):
                            raise ValueError("unsupported Braintrust overflow rows")
                        decoded = [json.loads(r) if isinstance(r, str) else r for r in rows]
                        with owner.lock:
                            owner.bt_rows.extend(decoded)
                            owner.exports.append({"path": path, "format": "braintrust", "payload": value, "received_ns": time.monotonic_ns(),
                                                  "body_b64": base64.b64encode(body).decode()})
                        self.reply({}); return
                    if "trace" in path:
                        spans = otlp_with_times(body, ctype)
                        with owner.lock:
                            owner.spans.extend(spans)
                            owner.exports.append({"path": path, "format": "otlp", "spans": spans, "received_ns": time.monotonic_ns(),
                                                  "body_b64": base64.b64encode(body).decode()})
                        self.reply({} if "json" in ctype else b"", content_type=ctype or "application/x-protobuf")
                        return
                    raise ValueError("unexpected collector path: " + path)
                except Exception as exc:
                    with owner.lock:
                        owner.collector_errors.append(f"{type(exc).__name__}: {exc}")
                    self.reply({"error": str(exc)}, status=400)

            do_POST = handle_request
            do_GET = handle_request
            do_PUT = handle_request

        self.provider.RequestHandlerClass = Provider
        self.collector.RequestHandlerClass = Collector
