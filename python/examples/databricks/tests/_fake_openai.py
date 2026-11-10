"""OpenAI-shaped fixtures and a loopback server; no upstream forwarding."""

import copy
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

ANSWER = "A lakehouse combines data lake storage with warehouse features."
USAGE = {"prompt_tokens": 7, "completion_tokens": 11, "total_tokens": 18}
AUTH_ERROR = {"error_code": "UNAUTHENTICATED", "message": "Invalid access token."}


def chat_response(model, usage=True):
    response = {
        "id": "chatcmpl-placeholder",
        "object": "chat.completion",
        "created": 1,
        "model": model,
        "choices": [{"index": 0, "message": {"role": "assistant", "content": ANSWER}, "finish_reason": "stop"}],
    }
    if usage:
        response["usage"] = dict(USAGE)
    return response


def stream_response(model, include_usage=False):
    chunks = []
    for index, text in enumerate(("A lakehouse combines ", "data lake storage with warehouse features.")):
        chunks.append({
            "id": "chatcmpl-placeholder", "object": "chat.completion.chunk", "created": 1,
            "model": model,
            "choices": [{"index": 0, "delta": {"content": text}, "finish_reason": "stop" if index else None}],
        })
    if include_usage:
        chunks.append({
            "id": "chatcmpl-placeholder", "object": "chat.completion.chunk", "created": 1,
            "model": model, "choices": [], "usage": dict(USAGE),
        })
    return ("".join("data: " + json.dumps(chunk) + "\n\n" for chunk in chunks) + "data: [DONE]\n\n").encode()


def embedding_response(model):
    return {
        "object": "list", "model": model,
        "data": [{"object": "embedding", "index": 0, "embedding": [0.125, 0.25, 0.5]}],
        "usage": {"prompt_tokens": 7, "total_tokens": 7},
    }


class FakeOpenAI:
    def __init__(self, error=False):
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
                if error:
                    status, content_type, payload = 401, "application/json", json.dumps(AUTH_ERROR).encode()
                elif body.get("stream"):
                    status, content_type = 200, "text/event-stream"
                    payload = stream_response(body["model"], body.get("stream_options", {}).get("include_usage", False))
                else:
                    status, content_type, payload = 200, "application/json", json.dumps(chat_response(body["model"])).encode()
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

    def requests(self):
        with self._lock:
            return copy.deepcopy(self._requests)

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self._server.shutdown()
        self._server.server_close()
        self._thread.join()
