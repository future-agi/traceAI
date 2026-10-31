"""Loopback-only provider + OTLP/HTTP receiver. No third-party dependencies."""
from __future__ import annotations

import gzip
import json
import socket
import struct
import threading
import time
import zlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

MODEL = "gpt-4o-mini"
RESPONSE_MODEL = "gpt-4o-mini-2024-07-18"
TOKENS = {"prompt_tokens": 11, "completion_tokens": 7, "total_tokens": 18}


def output_for(call_id: str) -> str:
    return f"reply/{call_id}/café/終"


def payload_for(call_id: str, stream: bool) -> list[bytes]:
    common = {"id": "chatcmpl-" + call_id, "model": RESPONSE_MODEL}
    def encode(value):
        return json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode()
    if not stream:
        return [encode({**common, "object": "chat.completion", "choices": [
            {"index": 0, "message": {"role": "assistant", "content": output_for(call_id)},
             "finish_reason": "stop"}], "usage": TOKENS})]
    events = [{**common, "object": "chat.completion.chunk", "choices": [
        {"index": 0, "delta": {"role": "assistant", "content": ""}, "finish_reason": None}]}]
    for i, text in enumerate(("reply/", call_id, "/café/終")):
        events.append({**common, "object": "chat.completion.chunk", "choices": [
            {"index": 0, "delta": {"content": text}, "finish_reason": "stop" if i == 2 else None}]})
    events.append({**common, "object": "chat.completion.chunk", "choices": [], "usage": TOKENS})
    frames = [b": loopback smoke\r\n\r\n"] + [b"data: " + encode(e) + b"\r\n\r\n" for e in events] + [b"data: [DONE]\r\n\r\n"]
    # Deliberately split a UTF-8 codepoint across writes; body bytes must be preserved.
    pos = frames[4].index("é".encode()) + 1
    frames[4:5] = [frames[4][:pos], frames[4][pos:]]
    return frames


def varint(data: bytes, pos: int) -> tuple[int, int]:
    value = 0
    for shift in range(0, 70, 7):
        if pos >= len(data):
            raise ValueError("truncated protobuf varint")
        b = data[pos]; pos += 1
        value |= (b & 127) << shift
        if b < 128:
            return value, pos
    raise ValueError("oversized protobuf varint")


def fields(data: bytes) -> dict[int, list]:
    result: dict[int, list] = {}
    pos = 0
    while pos < len(data):
        tag, pos = varint(data, pos)
        number, wire = tag >> 3, tag & 7
        if not number:
            raise ValueError("invalid protobuf field zero")
        if wire == 0:
            value, pos = varint(data, pos)
        elif wire in (1, 5):
            length = 8 if wire == 1 else 4
            value, pos = data[pos:pos + length], pos + length
            if len(value) != length:
                raise ValueError("truncated protobuf fixed field")
        elif wire == 2:
            length, pos = varint(data, pos)
            value, pos = data[pos:pos + length], pos + length
            if len(value) != length:
                raise ValueError("truncated protobuf string")
        else:
            raise ValueError(f"unsupported protobuf wire type {wire}")
        result.setdefault(number, []).append(value)
    return result


def any_proto(data: bytes):
    f = fields(data)
    if 1 in f: return f[1][0].decode()
    if 2 in f: return bool(f[2][0])
    if 3 in f:
        n = f[3][0]
        return n if n < 2**63 else n - 2**64
    if 4 in f: return struct.unpack("<d", f[4][0])[0]
    if 5 in f: return [any_proto(v) for v in fields(f[5][0]).get(1, [])]
    if 6 in f: return attributes_proto(fields(f[6][0]).get(1, []))
    if 7 in f: return f[7][0].hex()
    return None


def attributes_proto(values):
    result = {}
    for value in values:
        kv = fields(value)
        result[kv[1][0].decode()] = any_proto(kv[2][0])
    return result


def any_json(value):
    for key in ("stringValue", "string_value", "boolValue", "bool_value", "doubleValue", "double_value"):
        if key in value: return value[key]
    for key in ("intValue", "int_value"):
        if key in value: return int(value[key])
    for key in ("arrayValue", "array_value"):
        if key in value: return [any_json(v) for v in value[key].get("values", [])]
    for key in ("kvlistValue", "kvlist_value"):
        if key in value: return {v["key"]: any_json(v["value"]) for v in value[key].get("values", [])}
    return None


def decode_otlp(body: bytes, content_type: str) -> list[dict]:
    spans = []
    if "json" in content_type or body.lstrip().startswith(b"{"):
        root = json.loads(body)
        for resource in root.get("resourceSpans", root.get("resource_spans", [])):
            for scope in resource.get("scopeSpans", resource.get("scope_spans", resource.get("instrumentationLibrarySpans", []))):
                for span in scope.get("spans", []):
                    spans.append({"name": span.get("name"), "span_id": span.get("spanId", span.get("span_id")),
                                  "trace_id": span.get("traceId", span.get("trace_id")),
                                  "attributes": {a["key"]: any_json(a["value"]) for a in span.get("attributes", [])},
                                  "status": span.get("status", {})})
    else:
        for resource in fields(body).get(1, []):
            for scope in fields(resource).get(2, []):
                for value in fields(scope).get(2, []):
                    f = fields(value)
                    spans.append({"name": f.get(5, [b""])[0].decode(), "span_id": f.get(2, [b""])[0].hex(),
                                  "trace_id": f.get(1, [b""])[0].hex(),
                                  "attributes": attributes_proto(f.get(9, [])),
                                  "status": {"code": fields(f.get(15, [b""])[0]).get(3, [0])[0]}})
    return spans


class JoiningHTTPServer(ThreadingHTTPServer):
    # HTTPServer defaults to daemon handlers; opt out so server_close joins late
    # in-flight exports before correctness takes its final evidence snapshot.
    daemon_threads = False
    block_on_close = True


class LocalMocks:
    def __init__(self, chunk_delay_ms: float = 2):
        self.spans: list[dict] = []
        self.requests: list[dict] = []
        self.collector_errors: list[str] = []
        self.collector_requests: list[dict] = []
        self.first_span_ns: int | None = None
        self.lock = threading.Lock()
        owner = self

        class Base(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"
            def setup(self):
                super().setup()
                self.connection.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
                self.connection.settimeout(30)
            def log_message(self, *_): pass
            def read_entity(self):
                if self.headers.get("Transfer-Encoding", "").lower() == "chunked":
                    chunks, total = [], 0
                    while True:
                        line = self.rfile.readline(4096)
                        size = int(line.split(b";", 1)[0].strip(), 16)
                        if size == 0:
                            while self.rfile.readline(4096) not in (b"\r\n", b"\n", b""): pass
                            return b"".join(chunks)
                        total += size
                        if total > 64 * 1024 * 1024: raise ValueError("mock request exceeds 64 MiB")
                        data = self.rfile.read(size)
                        if len(data) != size or self.rfile.read(2) != b"\r\n":
                            raise ValueError("malformed HTTP chunk")
                        chunks.append(data)
                length = int(self.headers.get("Content-Length", 0))
                if length < 0 or length > 64 * 1024 * 1024: raise ValueError("invalid mock request length")
                return self.rfile.read(length)
            def reply(self, body, content_type="application/json", status=200):
                self.send_response(status)
                self.send_header("Content-Type", content_type)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body); self.wfile.flush()

        class Provider(Base):
            def do_GET(self):
                with owner.lock: owner.requests.append({"method": "GET", "path": self.path, "ai": False})
                self.reply(b'{"ok":true}')
            def do_POST(self):
                body = self.read_entity()
                value = json.loads(body)
                is_ai = "messages" in value and "model" in value
                call_id = value["messages"][0]["content"] if is_ai else None
                with owner.lock:
                    owner.requests.append({"method": "POST", "path": self.path, "ai": is_ai, "call_id": call_id,
                                           "model": value.get("model"), "stream": value.get("stream", False)})
                if not is_ai:
                    self.reply(b'{"ok":true}'); return
                stream = value.get("stream", False)
                frames = payload_for(call_id, stream)
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream; charset=utf-8" if stream else "application/json")
                self.send_header("Content-Length", str(sum(map(len, frames))))
                self.end_headers()
                for index, frame in enumerate(frames):
                    if stream and index: time.sleep(chunk_delay_ms / 1000)
                    self.wfile.write(frame); self.wfile.flush()

        class Collector(Base):
            def do_POST(self):
                try:
                    body = self.read_entity()
                    received_ns = time.monotonic_ns()
                    if self.headers.get("Content-Encoding") == "gzip": body = gzip.decompress(body)
                    spans = decode_otlp(body, self.headers.get("Content-Type", ""))
                    with owner.lock:
                        if spans and owner.first_span_ns is None: owner.first_span_ns = received_ns
                        owner.spans.extend(spans)
                        owner.collector_requests.append({"path": self.path, "content_type": self.headers.get("Content-Type"),
                                                         "content_encoding": self.headers.get("Content-Encoding"),
                                                         "decoded_bytes": len(body), "span_count": len(spans)})
                    if "json" in self.headers.get("Content-Type", ""):
                        self.reply(b"{}")
                    else:
                        self.reply(b"", "application/x-protobuf")
                except Exception as exc:
                    with owner.lock: owner.collector_errors.append(f"{type(exc).__name__}: {exc}")
                    self.reply(b"{}", status=400)

        self.provider = JoiningHTTPServer(("127.0.0.1", 0), Provider)
        self.collector = JoiningHTTPServer(("127.0.0.1", 0), Collector)
        self.threads = []

    @property
    def provider_url(self): return f"http://127.0.0.1:{self.provider.server_port}"
    @property
    def collector_url(self): return f"http://127.0.0.1:{self.collector.server_port}"
    def __enter__(self):
        for server in (self.provider, self.collector):
            thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.02}, daemon=True)
            thread.start(); self.threads.append(thread)
        return self
    def __exit__(self, *_):
        for server in (self.provider, self.collector): server.shutdown(); server.server_close()
        for thread in self.threads: thread.join()
