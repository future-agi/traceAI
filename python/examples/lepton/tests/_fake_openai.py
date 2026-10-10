"""OpenAI-shaped chat, SSE and error fixtures, served only on loopback."""

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

ANSWER = "A comet is an icy body that orbits the Sun."
PARTS = ("A comet is an icy body ", "that orbits the Sun.")
USAGE = {"prompt_tokens": 11, "completion_tokens": 13, "total_tokens": 24}
ERROR = {"error": {"message": "Unauthorized", "type": "authentication_error"}}


def completion(model, *, usage=True, answer=ANSWER):
    body = {
        "id": "chatcmpl-placeholder-lepton",
        "object": "chat.completion",
        "created": 1,
        "model": model,
        "choices": [{"index": 0, "message": {"role": "assistant", "content": answer}, "finish_reason": "stop"}],
    }
    if usage:
        body["usage"] = dict(USAGE)
    return body


def stream(model, *, include_usage=False):
    chunks = []
    for part in PARTS:
        chunks.append({
            "id": "chatcmpl-placeholder-lepton",
            "object": "chat.completion.chunk",
            "created": 1,
            "model": model,
            "choices": [{"index": 0, "delta": {"content": part}, "finish_reason": None}],
        })
    chunks.append({
        "id": "chatcmpl-placeholder-lepton",
        "object": "chat.completion.chunk",
        "created": 1,
        "model": model,
        "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
    })
    if include_usage:
        chunks.append({
            "id": "chatcmpl-placeholder-lepton",
            "object": "chat.completion.chunk",
            "created": 1,
            "model": model,
            "choices": [],
            "usage": dict(USAGE),
        })
    return ("".join(f"data: {json.dumps(chunk)}\n\n" for chunk in chunks) + "data: [DONE]\n\n").encode()


class FakeOpenAI:
    def __init__(self, *, error=False):
        self.requests = []
        self._lock = threading.Lock()
        owner = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                with owner._lock:
                    owner.requests.append({
                        "path": self.path,
                        "headers": {key.lower(): value for key, value in self.headers.items()},
                        "body": body,
                    })
                if self.path != "/v1/chat/completions":
                    self.send_error(404)
                    return
                if error:
                    status, content_type, payload = 401, "application/json", json.dumps(ERROR).encode()
                elif body.get("stream"):
                    status, content_type = 200, "text/event-stream"
                    payload = stream(body["model"], include_usage=body.get("stream_options", {}).get("include_usage", False))
                else:
                    status, content_type, payload = 200, "application/json", json.dumps(completion(body["model"])).encode()
                self.send_response(status)
                self.send_header("Content-Type", content_type)
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

            def log_message(self, _format, *args):
                pass

        self._server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.base_url = f"http://127.0.0.1:{self._server.server_port}/v1"
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        self._server.shutdown()
        self._server.server_close()
        self._thread.join()
