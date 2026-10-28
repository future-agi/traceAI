"""OpenAI-shaped fixtures and a loopback server; never forwards requests."""

import copy
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

ANSWER = "Paris is the capital of France."
STREAM_PARTS = ("Paris is ", "the capital ", "of France.")
USAGE = {"prompt_tokens": 9, "completion_tokens": 7, "total_tokens": 16}
ERROR = {"error": {"message": "Invalid API key", "type": "authentication_error"}}


def completion(model, *, usage=True):
    body = {
        "id": "chatcmpl-baseten-fixture", "object": "chat.completion",
        "created": 1, "model": model,
        "choices": [{"index": 0, "message": {"role": "assistant", "content": ANSWER},
                     "finish_reason": "stop"}],
    }
    if usage:
        body["usage"] = dict(USAGE)
    return body


def stream_body(model):
    chunks = []
    for index, text in enumerate(STREAM_PARTS):
        delta = {"content": text}
        if index == 0:
            delta["role"] = "assistant"
        chunks.append({
            "id": "chatcmpl-baseten-fixture", "object": "chat.completion.chunk",
            "created": 1, "model": model,
            "choices": [{"index": 0, "delta": delta, "finish_reason": None}],
        })
    chunks.append({
        "id": "chatcmpl-baseten-fixture", "object": "chat.completion.chunk",
        "created": 1, "model": model,
        "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
    })
    return ("".join(f"data: {json.dumps(chunk)}\n\n" for chunk in chunks)
            + "data: [DONE]\n\n").encode()


class FakeOpenAI:
    def __init__(self):
        self._requests = []
        self._lock = threading.Lock()
        owner = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                with owner._lock:
                    owner._requests.append({
                        "path": self.path,
                        "headers": {key.lower(): value for key, value in self.headers.items()},
                        "body": body,
                    })
                if self.path != "/v1/chat/completions":
                    self.send_error(404)
                    return
                unauthorized = self.headers.get("Authorization") != (
                    "Bearer placeholder-baseten-key"
                )
                is_stream = body.get("stream", False) and not unauthorized
                if unauthorized:
                    payload = json.dumps(ERROR).encode()
                elif is_stream:
                    payload = stream_body(body["model"])
                else:
                    payload = json.dumps(completion(body["model"])).encode()
                self.send_response(401 if unauthorized else 200)
                self.send_header("Content-Type", "text/event-stream" if is_stream
                                 else "application/json")
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

    def requests(self):
        with self._lock:
            return copy.deepcopy(self._requests)
