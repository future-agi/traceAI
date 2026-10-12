"""OpenAI-shaped fixtures and a recording server that only binds to loopback."""

import copy
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

REQUEST_MODEL = "openai/gpt-5-mini"
RESPONSE_MODEL = "openai/gpt-5-mini-2026-09-01"  # Synthetic resolved-model fixture.
ANSWER = "Loopback fixture answer."
RAW_MARKER = "raw-response-extra-fixture-marker"
CHAT_USAGE = {"prompt_tokens": 11, "completion_tokens": 5, "total_tokens": 16}
STREAM_USAGE = {"prompt_tokens": 17, "completion_tokens": 7, "total_tokens": 24}


def completion(*, usage=True, answer=ANSWER):
    result = {
        "id": "chatcmpl-loopback-fixture",
        "object": "chat.completion",
        "created": 1,
        "model": RESPONSE_MODEL,
        "choices": [{"index": 0, "message": {"role": "assistant", "content": answer}, "finish_reason": "stop"}],
        "fixture_marker": RAW_MARKER,
    }
    if usage:
        result["usage"] = dict(CHAT_USAGE)
    return result


def stream_body(*, include_usage=False, answer=ANSWER, no_text=False):
    chunks = []
    for index, content in enumerate((answer[:9], answer[9:]) if not no_text else (None,)):
        delta = {"role": "assistant"} if index == 0 else {}
        if content is not None:
            delta["content"] = content
        chunks.append({
            "id": "chatcmpl-loopback-fixture",
            "object": "chat.completion.chunk",
            "created": 1,
            "model": RESPONSE_MODEL,
            "choices": [{"index": 0, "delta": delta, "finish_reason": None}],
            "fixture_marker": RAW_MARKER,
        })
    chunks.append({
        "id": "chatcmpl-loopback-fixture",
        "object": "chat.completion.chunk",
        "created": 1,
        "model": RESPONSE_MODEL,
        "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
    })
    if include_usage:
        chunks.append({
            "id": "chatcmpl-loopback-fixture",
            "object": "chat.completion.chunk",
            "created": 1,
            "model": RESPONSE_MODEL,
            "choices": [],
            "usage": dict(STREAM_USAGE),
        })
    return ("".join("data: " + json.dumps(chunk) + "\n\n" for chunk in chunks) + "data: [DONE]\n\n").encode()


def error_body(status):
    return {"error": {"message": "Invalid API key." if status == 401 else "Insufficient credits.", "type": "invalid_request_error"}}


class FakeOpenAI:
    def __init__(self, status=200):
        self.status = status
        self._requests = []
        self._lock = threading.Lock()
        owner = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", "0"))))
                with owner._lock:
                    owner._requests.append({"path": self.path, "headers": {k.lower(): v for k, v in self.headers.items()}, "body": body})
                if self.path != "/v1/chat/completions":
                    self.send_error(404)
                    return
                if owner.status != 200:
                    payload = json.dumps(error_body(owner.status)).encode()
                    content_type = "application/json"
                elif body.get("stream"):
                    payload = stream_body(include_usage=body.get("stream_options", {}).get("include_usage", False))
                    content_type = "text/event-stream"
                else:
                    payload = json.dumps(completion()).encode()
                    content_type = "application/json"
                self.send_response(owner.status)
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

    def __exit__(self, *_args):
        self._server.shutdown()
        self._server.server_close()
        self._thread.join()
