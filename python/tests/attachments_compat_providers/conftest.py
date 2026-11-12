"""Loopback harness for provider-media tests: synthetic data only.

Reuses the F01 server, span exporter and media builders of
``tests/attachments_providers/conftest.py`` (loaded by path, so both suites
share one harness), and adds a plain-HTTP provider that answers like Gemini,
Vertex (REST), Bedrock Runtime, Ollama and OpenAI-compatible chat APIs, so the
REAL installed SDKs run end to end with outbound network disabled.
"""

import base64
import importlib.util
import json
import os
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import unquote

import pytest

# LiteLLM fetches its model cost map at import unless told to use the bundled copy.
os.environ.setdefault("LITELLM_LOCAL_MODEL_COST_MAP", "True")
os.environ.setdefault("AWS_EC2_METADATA_DISABLED", "true")

pytest.importorskip("fi_instrumentation.attachments.provider_media")

_spec = importlib.util.spec_from_file_location(
    "_attachment_provider_harness", Path(__file__).parent.parent / "attachments_providers" / "conftest.py")
harness = importlib.util.module_from_spec(_spec)
sys.modules["_attachment_provider_harness"] = harness
_spec.loader.exec_module(harness)

# Fixtures (pytest finds them by name in this module) and helpers.
f01 = harness.f01
spans = harness.spans
clean_env = harness.clean_env
png, wav, PDF, b64, refs, flush, wait_uploads = (harness.png, harness.wav, harness.PDF, harness.b64, harness.refs,
                                                  harness.flush, harness.wait_uploads)


def jpeg(seed=0) -> bytes:
    """Leading bytes of a JPEG (SOI + APP0); the loopback F01 server does not decode it."""
    return b"\xff\xd8\xff\xe0\x00\x10JFIF\x00\x01\x01\x00\x00\x01\x00\x01\x00\x00" + bytes([seed % 256]) * 32 + b"\xff\xd9"


MP4 = b"\x00\x00\x00\x18ftypisom\x00\x00\x02\x00isomiso2" + b"\x00" * 24


def data_uri(data: bytes, mime="image/png") -> str:
    return "data:%s;base64,%s" % (mime, b64(data))


def uploads_by_purpose(f01_server):
    found = {}
    for upload in f01_server.uploads:
        found.setdefault(upload.get("purpose"), []).append(upload)
    return found


def span_named(exporter, *names):
    matches = [span for span in exporter.get_finished_spans() if span.name in names]
    assert matches, [span.name for span in exporter.get_finished_spans()]
    return matches[-1]


def all_refs(exporter):
    found = []
    for span in exporter.get_finished_spans():
        for ref in refs(span):
            found.append((span.name, ref))
    return found


class MediaProviderState:
    def __init__(self):
        self.requests = []
        self.image = png(seed=21)
        self.generated = png(seed=22)


def _openai_chat(body):
    if body.get("stream"):
        chunks = [{"id": "c1", "object": "chat.completion.chunk", "created": 1, "model": "m",
                   "choices": [{"index": 0, "delta": {"role": "assistant", "content": "ok"}, "finish_reason": None}]},
                  {"id": "c1", "object": "chat.completion.chunk", "created": 1, "model": "m",
                   "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
                   "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2}}]
        return "text/event-stream", "".join("data: %s\n\n" % json.dumps(c) for c in chunks).encode() + b"data: [DONE]\n\n"
    return "application/json", json.dumps({
        "id": "c1", "object": "chat.completion", "created": 1, "model": body.get("model", "m"),
        "choices": [{"index": 0, "message": {"role": "assistant", "content": "ok"}, "finish_reason": "stop"}],
        "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2}}).encode()


def _gemini(state, stream):
    response = {"candidates": [{"content": {"role": "model", "parts": [
        {"text": "ok"}, {"inlineData": {"mimeType": "image/png", "data": b64(state.generated)}}]},
        "finishReason": "STOP", "index": 0}],
        "usageMetadata": {"promptTokenCount": 1, "candidatesTokenCount": 1, "totalTokenCount": 2},
        "modelVersion": "synthetic"}
    if stream:
        return "text/event-stream", ("data: %s\r\n\r\n" % json.dumps(response)).encode()
    return "application/json", json.dumps(response).encode()


def _bedrock(state, path, body):
    model = unquote(path.split("/model/", 1)[1].rsplit("/", 1)[0])
    if path.endswith("/converse"):
        return "application/json", json.dumps({
            "output": {"message": {"role": "assistant", "content": [{"text": "ok"}]}}, "stopReason": "end_turn",
            "usage": {"inputTokens": 1, "outputTokens": 1, "totalTokens": 2}, "metrics": {"latencyMs": 1}}).encode()
    if "titan-image" in model or "nova-canvas" in model:
        return "application/json", json.dumps({"images": [b64(state.generated)], "error": None}).encode()
    if "anthropic" in model:
        return "application/json", json.dumps({
            "id": "msg_1", "type": "message", "role": "assistant", "model": model,
            "content": [{"type": "text", "text": "ok"}], "stop_reason": "end_turn", "stop_sequence": None,
            "usage": {"input_tokens": 1, "output_tokens": 1}}).encode()
    return "application/json", json.dumps({
        "output": {"message": {"role": "assistant", "content": [{"text": "ok"}]}}, "stopReason": "end_turn",
        "usage": {"inputTokens": 1, "outputTokens": 1, "totalTokens": 2}}).encode()


def _ollama(path, body):
    done = {"model": body.get("model", "m"), "created_at": "2025-01-01T00:00:00Z", "done": True,
            "done_reason": "stop", "total_duration": 1, "load_duration": 1, "prompt_eval_count": 1,
            "prompt_eval_duration": 1, "eval_count": 1, "eval_duration": 1}
    if path.endswith("/api/chat"):
        done["message"] = {"role": "assistant", "content": "ok"}
    else:
        done["response"] = "ok"
    if body.get("stream", True):
        return "application/x-ndjson", (json.dumps(done) + "\n").encode()
    return "application/json", json.dumps(done).encode()


@pytest.fixture
def media_provider():
    state = MediaProviderState()

    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, *args):
            pass

        def send(self, content_type, data, status=200):
            self.send_response(status)
            self.send_header("Content-Type", content_type)
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

        def do_GET(self):
            self.send("application/json", b"{}")

        def do_HEAD(self):
            self.send_response(200)
            self.send_header("Content-Length", "0")
            self.end_headers()

        def do_POST(self):
            raw = self.body()
            try:
                body = json.loads(raw) if raw else {}
            except ValueError:
                body = {}
            state.requests.append((self.path, body, raw))
            path = self.path.split("?")[0]
            if path.endswith(":generateContent"):
                self.send(*_gemini(state, False))
            elif path.endswith(":streamGenerateContent"):
                self.send(*_gemini(state, True))
            elif path.endswith(":predict"):
                self.send("application/json", json.dumps({"predictions": [
                    {"bytesBase64Encoded": b64(state.generated), "mimeType": "image/png"}],
                    "deployedModelId": "synthetic"}).encode())
            elif "/model/" in path and path.endswith(("/converse", "/invoke")):
                self.send(*_bedrock(state, path, body))
            elif path.endswith(("/api/chat", "/api/generate")):
                self.send(*_ollama(path, body))
            elif path.endswith("/chat/completions"):
                self.send(*_openai_chat(body))
            elif path.endswith("/images/generations"):
                self.send("application/json", json.dumps({"created": 1, "data": [
                    {"b64_json": b64(state.generated)}, {"url": "https://images.invalid/x.png"}]}).encode())
            else:
                self.send("application/json", b"{}", status=404)

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    state.url = "http://127.0.0.1:%d" % server.server_port
    yield state
    server.shutdown()
    server.server_close()
    thread.join(2)


def decoded(value: str) -> bytes:
    return base64.b64decode(value)
