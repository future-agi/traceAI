"""OpenAI-shaped fixtures and a loopback server; never forwards requests."""

import copy
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

ANSWER = "Hello from local Qwen."
MODEL = "qwen-plus"
USAGE = {"prompt_tokens": 5, "completion_tokens": 7, "total_tokens": 12}
AUTH_ERROR = {
    "error": {
        "message": "Incorrect API key provided. ",
        "type": "invalid_request_error",
        "param": None,
        "code": "invalid_api_key",
    }
}


def completion(model=MODEL, include_usage=True):
    body = {
        "id": "chatcmpl-local-qwen",
        "object": "chat.completion",
        "created": 1,
        "model": model,
        "choices": [
            {
                "index": 0,
                "message": {"role": "assistant", "content": ANSWER},
                "finish_reason": "stop",
            }
        ],
    }
    if include_usage:
        body["usage"] = USAGE.copy()
    return body


def stream_body(model=MODEL, include_usage=False):
    chunks = []
    for index, content in enumerate(("Hello ", "from local ", "Qwen.")):
        delta = {"content": content}
        if index == 0:
            delta["role"] = "assistant"
        chunks.append(
            {
                "id": "chatcmpl-local-qwen",
                "object": "chat.completion.chunk",
                "created": 1,
                "model": model,
                "choices": [{"index": 0, "delta": delta, "finish_reason": None}],
            }
        )
    chunks.append(
        {
            "id": "chatcmpl-local-qwen",
            "object": "chat.completion.chunk",
            "created": 1,
            "model": model,
            "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
        }
    )
    if include_usage:
        chunks.append({**chunks[-1], "choices": [], "usage": USAGE.copy()})
    return ("".join(f"data: {json.dumps(chunk)}\n\n" for chunk in chunks) + "data: [DONE]\n\n").encode()


class FakeOpenAI:
    def __init__(self, error=False):
        self._requests = []
        self._lock = threading.Lock()
        owner = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                with owner._lock:
                    owner._requests.append(
                        {
                            "path": self.path,
                            "headers": {key.lower(): value for key, value in self.headers.items()},
                            "body": body,
                        }
                    )
                if self.path != "/compatible-mode/v1/chat/completions":
                    self.send_error(404)
                    return
                if error:
                    status, content_type, payload = 401, "application/json", json.dumps(AUTH_ERROR).encode()
                elif body.get("stream"):
                    status, content_type = 200, "text/event-stream"
                    payload = stream_body(
                        body["model"], body.get("stream_options", {}).get("include_usage", False)
                    )
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
        self.base_url = f"http://127.0.0.1:{self._server.server_port}/compatible-mode/v1"
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)

    def __enter__(self):
        self._thread.start()
        return self

    def __exit__(self, *_args):
        self._server.shutdown()
        self._server.server_close()
        self._thread.join()

    def requests(self):
        with self._lock:
            return copy.deepcopy(self._requests)
