"""OpenAI-shaped fixtures and a recording server bound only to 127.0.0.1."""

import copy
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

REQUESTED_MODEL = "orcarouter/auto"
RESOLVED_MODEL = "deepseek/deepseek-chat-2026-09-01"  # Synthetic resolved-model fixture.
ANSWER = "A coral reef is a marine ecosystem."
USAGE = {"prompt_tokens": 13, "completion_tokens": 7, "total_tokens": 20}


def completion(*, usage=True, text=ANSWER, extra=None):
    result = {
        "id": "chatcmpl-fixture", "object": "chat.completion", "created": 1,
        "model": RESOLVED_MODEL,
        "choices": [{"index": 0, "message": {"role": "assistant", "content": text}, "finish_reason": "stop"}],
    }
    if usage:
        result["usage"] = dict(USAGE)
    if extra:
        result.update(extra)
    return result


def stream_bytes(*, include_usage=False, text=ANSWER):
    chunks = []
    for delta in ({"role": "assistant", "content": text[:16]}, {"content": text[16:]}, {}):
        chunks.append({
            "id": "chatcmpl-fixture", "object": "chat.completion.chunk", "created": 1,
            "model": RESOLVED_MODEL,
            "choices": [{"index": 0, "delta": delta, "finish_reason": "stop" if not delta else None}],
        })
    if include_usage:
        chunks.append({
            "id": "chatcmpl-fixture", "object": "chat.completion.chunk", "created": 1,
            "model": RESOLVED_MODEL, "choices": [], "usage": dict(USAGE),
        })
    return ("".join("data: " + json.dumps(chunk) + "\n\n" for chunk in chunks) + "data: [DONE]\n\n").encode()


def error_body(status):
    return {"error": {
        "message": "Fixture authentication refused" if status == 401 else "Fixture rate limit exceeded",
        "type": "authentication_error" if status == 401 else "rate_limit_error",
        "code": "invalid_api_key" if status == 401 else "rate_limit_exceeded",
    }}


class FakeOpenAI:
    def __init__(self, *, status=200):
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
                if status != 200:
                    payload = json.dumps(error_body(status)).encode()
                    content_type = "application/json"
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

            def log_message(self, _format, *args):
                pass

        self._server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.origin = f"http://127.0.0.1:{self._server.server_port}"
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
