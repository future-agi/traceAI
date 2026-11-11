"""Loopback harness for provider attachment tests: synthetic data only.

- ``f01``: a TLS loopback F01 attachment server (sessions, uploads, content,
  complete). It records every request and uploaded payload.
- ``provider``: a plain-HTTP loopback that answers like the OpenAI, Anthropic
  and Groq APIs (JSON and SSE), so the REAL installed SDKs run end to end.
- ``spans``: an OpenTelemetry provider with an in-memory exporter.
"""

import base64
import io
import json
import os
import ssl
import struct
import subprocess
import sys
import threading
import uuid
import wave
import zlib
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))

pytest.importorskip("fi_instrumentation.attachments.media_capture")

from fi_instrumentation.attachments import AttachmentConfig, AttachmentManager  # noqa: E402
from fi_instrumentation.attachments.protocol import HTTPTransport  # noqa: E402
from opentelemetry.sdk.trace import TracerProvider  # noqa: E402
from opentelemetry.sdk.trace.export import SimpleSpanProcessor  # noqa: E402
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter  # noqa: E402


def png(width=2, height=2, seed=0) -> bytes:
    """A small valid PNG; ``seed`` changes the pixels so payloads are distinguishable."""
    raw = b"".join(b"\x00" + bytes((seed + x + y) % 256 for x in range(width * 3)) for y in range(height))

    def chunk(kind, data):
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data))

    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(raw)) + chunk(b"IEND", b""))


def wav(frames=800, seed=0) -> bytes:
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as writer:
        writer.setnchannels(1)
        writer.setsampwidth(2)
        writer.setframerate(8000)
        writer.writeframes(bytes((seed + i) % 256 for i in range(frames * 2)))
    return buffer.getvalue()


PDF = b"%PDF-1.4\n1 0 obj<<>>endobj\ntrailer<<>>\n%%EOF\n"


def b64(data: bytes) -> str:
    return base64.b64encode(data).decode()


def expiry(seconds=600):
    return (datetime.now(timezone.utc) + timedelta(seconds=seconds)).isoformat()


@pytest.fixture(autouse=True)
def clean_env(monkeypatch):
    for key in list(os.environ):
        if key.startswith(("FI_ATTACHMENTS_", "FI_HIDE_")) or key in ("FI_PII_REDACTION",):
            monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("FI_BASE_URL", "http://127.0.0.1:9")
    monkeypatch.setenv("FI_GRPC_URL", "http://127.0.0.1:9")
    for key in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "all_proxy"):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("NO_PROXY", "127.0.0.1,localhost")


class F01:
    def __init__(self, server, thread, cert, state):
        self.server, self.thread, self.cert, self.state = server, thread, cert, state

    @property
    def endpoint(self):
        return "https://localhost:%d" % self.server.server_port

    def transport(self):
        return HTTPTransport(self.endpoint, {"X-API-Key": "SYNTHETIC_KEY", "X-Secret-Key": "SYNTHETIC_SECRET"},
                             ssl_context=ssl.create_default_context(cafile=str(self.cert)))

    def manager(self, **config):
        manager = AttachmentManager(config=AttachmentConfig(enabled=True, **config), project_name="synthetic",
                                    transport=self.transport())
        assert manager.ready(5), manager.status()
        return manager

    @property
    def uploads(self):
        """Initialize bodies (POST .../uploads) in arrival order."""
        return [body for path, body in self.state["requests"] if path.endswith("/uploads")]

    @property
    def payloads(self):
        return list(self.state["payloads"])

    def after_bootstrap(self):
        return [path for path, _ in self.state["requests"] if not path.endswith("attachment-sessions")]


@pytest.fixture
def f01(tmp_path):
    key, cert = tmp_path / "synthetic.key", tmp_path / "synthetic.crt"
    subprocess.run(["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-keyout", str(key), "-out",
                    str(cert), "-days", "1", "-subj", "/CN=localhost", "-addext", "subjectAltName=DNS:localhost"],
                   check=True, capture_output=True)
    lock = threading.Lock()
    state = {"requests": [], "payloads": [], "uploads": {}, "policy": {}}
    scope = {k: str(uuid.uuid4()) for k in ("organization_id", "workspace_id", "project_id")}

    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, *args):
            pass

        def respond(self, status, body):
            data = json.dumps(body).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)) or b"{}")
            with lock:
                state["requests"].append((self.path, body))
            if self.path.endswith("attachment-sessions"):
                policy = {"version": "v1", "enabled": True, "allowed_sources": ["Bytes"],
                          "allowed_modalities": ["audio", "image", "document", "file"],
                          "require_media_redaction": False, "require_metadata_removal": False,
                          "allow_unredacted_media_with_text_pii": False, "record_audio": False,
                          "record_video": False, **state["policy"]}
                self.respond(200, {"schema_version": 1, "scope": scope,
                                   "capture_session_id": body["capture_session_id"], "expires_at": expiry(),
                                   "transfer_modes": ["authenticated_stream"], "policy": policy,
                                   "limits": {"max_upload_bytes": 8388608, "segment_bytes": 8388608,
                                              "segment_duration_ms": 30000, "recording_bytes": 1073741824,
                                              "recording_duration_ms": 7200000, "active_transfers": 16,
                                              "active_recordings": 4, "reserved_bytes": 1073741824,
                                              "validation_jobs": 1024, "initializations_per_minute": 120,
                                              "links_per_minute": 1000, "upload_bytes_per_second": 8388608,
                                              "playback_bytes_per_second": 1048576, "transfer_timeout_seconds": 30,
                                              "grant_ttl_seconds": 60, "revocation_check_seconds": 5,
                                              **state.get("limits", {})}})
            elif self.path.endswith("/uploads"):
                upload_id = str(uuid.uuid4())
                with lock:
                    state["uploads"][upload_id] = body
                self.respond(201, {"upload_id": upload_id, "attachment_id": body["attachment_id"],
                                   "occurrence_id": body["occurrence_id"], "state": "pending_upload",
                                   "reserved_bytes": body["declared_bytes"], "expires_at": expiry(60),
                                   "transfer_mode": "authenticated_stream",
                                   "write_capability": "SYNTHETIC_UPLOAD_ONLY"})
            elif self.path.endswith("/complete"):
                upload_id = self.path.rstrip("/").split("/")[-2]
                self.respond(202, {"upload_id": upload_id, "state": "validating", "reason": "binding_pending"})
            elif self.path.endswith("/abort"):
                upload_id = self.path.rstrip("/").split("/")[-2]
                self.respond(200, {"upload_id": upload_id, "state": "failed", "reason": "incomplete"})
            else:
                self.respond(404, {})

        def do_PUT(self):
            payload = self.rfile.read(int(self.headers.get("Content-Length") or 0))
            upload_id = self.path.rstrip("/").split("/")[-2]
            with lock:
                state["requests"].append((self.path, None))
                state["payloads"].append(payload)
            self.respond(200, {"upload_id": upload_id, "state": "pending_upload", "reason": "binding_pending"})

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(certfile=str(cert), keyfile=str(key))
    server.socket = context.wrap_socket(server.socket, server_side=True)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield F01(server, thread, cert, state)
    server.shutdown()
    server.server_close()
    thread.join(2)


# ------------------------------------------------------------------ provider
def _sse(events):
    return "".join("event: %s\ndata: %s\n\n" % (event.get("type", "message"), json.dumps(event))
                   for event in events).encode()


class ProviderState:
    def __init__(self):
        self.requests = []
        self.speech = wav(seed=7)
        self.image = png(seed=9)
        self.speech_chunk = 256


def _chat(body):
    if body.get("stream"):
        chunks = [{"id": "c1", "object": "chat.completion.chunk", "created": 1, "model": "m",
                   "choices": [{"index": 0, "delta": {"role": "assistant", "content": "ok"}, "finish_reason": None}]},
                  {"id": "c1", "object": "chat.completion.chunk", "created": 1, "model": "m",
                   "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]}]
        return "text/event-stream", "".join("data: %s\n\n" % json.dumps(c) for c in chunks).encode() + b"data: [DONE]\n\n"
    return "application/json", json.dumps({
        "id": "c1", "object": "chat.completion", "created": 1, "model": "m",
        "choices": [{"index": 0, "message": {"role": "assistant", "content": "ok"}, "finish_reason": "stop"}],
        "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2}}).encode()


def _response(state, body, status="completed"):
    output = [{"type": "message", "id": "m1", "status": "completed", "role": "assistant",
               "content": [{"type": "output_text", "text": "ok", "annotations": []}]}]
    if body.get("tools") and any(t.get("type") == "image_generation" for t in body["tools"]):
        output.append({"type": "image_generation_call", "id": "ig1", "status": "completed",
                       "result": b64(state.image)})
    return {"id": "r1", "object": "response", "created_at": 1, "model": "m", "status": status, "output": output,
            "parallel_tool_calls": True, "tool_choice": "auto", "tools": [], "error": None,
            "incomplete_details": None, "instructions": None, "metadata": {}, "temperature": 1.0, "top_p": 1.0,
            "usage": {"input_tokens": 1, "output_tokens": 1, "total_tokens": 2,
                      "input_tokens_details": {"cached_tokens": 0},
                      "output_tokens_details": {"reasoning_tokens": 0}}}


def _responses(state, body):
    if body.get("stream"):
        final = _response(state, body)
        created = dict(final, status="in_progress", output=[])
        events = [{"type": "response.created", "sequence_number": 0, "response": created}]
        if any(item["type"] == "image_generation_call" for item in final["output"]):
            events.append({"type": "response.image_generation_call.partial_image", "sequence_number": 1,
                           "item_id": "ig1", "output_index": 1, "partial_image_index": 0,
                           "partial_image_b64": b64(png(seed=3))})
        events.append({"type": "response.completed", "sequence_number": 2, "response": final})
        return "text/event-stream", _sse(events)
    return "application/json", json.dumps(_response(state, body)).encode()


def _anthropic(body):
    message = {"id": "msg_1", "type": "message", "role": "assistant", "model": body.get("model", "m"),
               "content": [{"type": "text", "text": "ok"}], "stop_reason": "end_turn", "stop_sequence": None,
               "usage": {"input_tokens": 1, "output_tokens": 1}}
    if body.get("stream"):
        events = [{"type": "message_start", "message": dict(message, content=[])},
                  {"type": "content_block_start", "index": 0, "content_block": {"type": "text", "text": ""}},
                  {"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": "ok"}},
                  {"type": "content_block_stop", "index": 0},
                  {"type": "message_delta", "delta": {"stop_reason": "end_turn", "stop_sequence": None},
                   "usage": {"output_tokens": 1}},
                  {"type": "message_stop"}]
        return "text/event-stream", _sse(events)
    return "application/json", json.dumps(message).encode()


@pytest.fixture
def provider():
    state = ProviderState()

    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, *args):
            pass

        def send(self, content_type, data, chunked=False):
            self.send_response(200)
            self.send_header("Content-Type", content_type)
            if chunked:
                self.send_header("Transfer-Encoding", "chunked")
                self.end_headers()
                for offset in range(0, len(data), state.speech_chunk):
                    piece = data[offset:offset + state.speech_chunk]
                    self.wfile.write(b"%x\r\n%s\r\n" % (len(piece), piece))
                self.wfile.write(b"0\r\n\r\n")
            else:
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

        def body(self):
            if self.headers.get("Transfer-Encoding", "").lower() == "chunked":
                parts = []
                while True:
                    size = int(self.rfile.readline().split(b";")[0].strip() or b"0", 16)
                    if not size:
                        self.rfile.readline()
                        return b"".join(parts)
                    parts.append(self.rfile.read(size))
                    self.rfile.readline()
            return self.rfile.read(int(self.headers.get("Content-Length") or 0))

        def do_POST(self):
            raw = self.body()
            content_type = self.headers.get("Content-Type", "")
            body = json.loads(raw) if content_type.startswith("application/json") and raw else {}
            state.requests.append((self.path, body, raw))
            path = self.path.split("?")[0]
            if path.endswith("/chat/completions"):
                self.send(*_chat(body))
            elif path.endswith("/responses"):
                self.send(*_responses(state, body))
            elif path.endswith("/images/generations"):
                self.send("application/json", json.dumps({"created": 1, "data": [
                    {"b64_json": b64(state.image)}, {"url": "https://images.invalid/x.png"}]}).encode())
            elif path.endswith("/audio/speech"):
                self.send("audio/wav", state.speech, chunked=True)
            elif path.endswith(("/audio/transcriptions", "/audio/translations")):
                self.send("application/json", json.dumps({"text": "hello"}).encode())
            elif path.endswith("/messages"):
                self.send(*_anthropic(body))
            else:
                self.send_response(404)
                self.send_header("Content-Length", "0")
                self.end_headers()

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    state.url = "http://127.0.0.1:%d" % server.server_port
    yield state
    server.shutdown()
    server.server_close()
    thread.join(2)


@pytest.fixture
def spans():
    exporter = InMemorySpanExporter()
    tracer_provider = TracerProvider()
    tracer_provider.add_span_processor(SimpleSpanProcessor(exporter))
    yield tracer_provider, exporter
    tracer_provider.shutdown()


def refs(span):
    raw = (span.attributes or {}).get("fi.attachment.refs")
    return json.loads(raw) if raw else []


def wait_uploads(f01, count, timeout=10.0):
    import time

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if len(f01.payloads) >= count:
            return
        time.sleep(0.02)


def flush(manager):
    result = manager.flush(10)
    assert result.unfinished == 0, result
