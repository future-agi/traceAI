"""OpenAI-shaped fixtures and a recording server that binds only to 127.0.0.1."""

import copy
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

# Historical example from the public tracing guide; availability is not asserted.
MODEL = "deepseek-ai/DeepSeek-V4-Flash"
RETURNED_MODEL = MODEL + "-2026-09-01"  # Synthetic dated response id.
ANSWER = "A rainbow is refracted light."
USAGE = {"prompt_tokens": 13, "completion_tokens": 7, "total_tokens": 20}
VECTOR = [0.125, -0.75, 0.5]


def completion(*, usage=True, text=ANSWER, extra=None):
    body = {
        "id": "chatcmpl-fixture",
        "object": "chat.completion",
        "created": 1725148800,
        "model": RETURNED_MODEL,
        "choices": [{"index": 0, "message": {"role": "assistant", "content": text},
                     "finish_reason": "stop"}],
    }
    if usage:
        body["usage"] = dict(USAGE)
    if extra:
        body.update(extra)
    return body


def stream_bytes(*, include_usage=False, text=ANSWER, extra=None):
    chunks = []
    for piece in (text[:10], text[10:]):
        chunk = {
            "id": "chatcmpl-fixture",
            "object": "chat.completion.chunk",
            "created": 1725148800,
            "model": RETURNED_MODEL,
            "choices": [{"index": 0, "delta": {"content": piece}, "finish_reason": None}],
        }
        if extra:
            chunk.update(extra)
        chunks.append(chunk)
    chunks.append({
        "id": "chatcmpl-fixture", "object": "chat.completion.chunk",
        "created": 1725148800, "model": RETURNED_MODEL,
        "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
    })
    if include_usage:
        chunks.append({
            "id": "chatcmpl-fixture", "object": "chat.completion.chunk",
            "created": 1725148800, "model": RETURNED_MODEL,
            "choices": [], "usage": dict(USAGE),
        })
    return ("".join(f"data: {json.dumps(chunk)}\n\n" for chunk in chunks)
            + "data: [DONE]\n\n").encode()


def error_body(status):
    return {"error": {
        "message": "Invalid API key" if status == 401 else "Rate limit exceeded",
        "type": "authentication_error" if status == 401 else "rate_limit_error",
        "code": "invalid_api_key" if status == 401 else "rate_limit_exceeded",
    }}


def embeddings():
    return {
        "object": "list", "model": "placeholder-embedding-model-2026-09-01",
        "data": [{"object": "embedding", "index": 0, "embedding": list(VECTOR)}],
        "usage": {"prompt_tokens": 11, "total_tokens": 11},
    }


class FakeOpenAI:
    def __init__(self, status=200):
        self._requests = []
        self._lock = threading.Lock()
        owner = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                with owner._lock:
                    owner._requests.append({
                        "path": self.path, "body": body,
                        "headers": {k.lower(): v for k, v in self.headers.items()},
                    })
                if status != 200:
                    payload = json.dumps(error_body(status)).encode()
                    content_type = "application/json"
                elif self.path != "/v1/chat/completions":
                    self.send_error(404)
                    return
                elif body.get("stream"):
                    payload = stream_bytes(include_usage=body.get("stream_options", {}).get("include_usage", False))
                    content_type = "text/event-stream"
                else:
                    payload = json.dumps(completion()).encode()
                    content_type = "application/json"
                self.send_response(status)
                self.send_header("Content-Type", content_type)
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

            def log_message(self, *args):
                pass

        self._server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.origin = f"http://127.0.0.1:{self._server.server_port}"
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)

    def __enter__(self):
        self._thread.start()
        return self

    def __exit__(self, *args):
        self._server.shutdown()
        self._server.server_close()
        self._thread.join()

    def requests(self):
        with self._lock:
            return copy.deepcopy(self._requests)
