"""OpenAI-shaped fixtures and a local server; no upstream requests."""

import copy
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

ANSWER = "A rainbow is light split into colors."
CHAT_USAGE = {"prompt_tokens": 7, "completion_tokens": 9, "total_tokens": 16}
ERROR = {"error": {"code": "401", "message": "Access denied due to invalid subscription key or wrong API endpoint."}}


def chat_response(model, usage=True):
    response = {
        "id": "chatcmpl-fixture", "object": "chat.completion", "created": 1,
        "model": model,
        "choices": [{"index": 0, "message": {"role": "assistant", "content": ANSWER}, "finish_reason": "stop"}],
    }
    if usage:
        response["usage"] = dict(CHAT_USAGE)
    return response


def chat_stream(model, include_usage=False):
    chunks = []
    for index, text in enumerate(("A rainbow is ", "light split into colors.")):
        chunks.append({
            "id": "chatcmpl-fixture", "object": "chat.completion.chunk", "created": 1,
            "model": model,
            "choices": [{"index": 0, "delta": {"content": text, **({"role": "assistant"} if index == 0 else {})}, "finish_reason": None}],
        })
    chunks.append({
        "id": "chatcmpl-fixture", "object": "chat.completion.chunk", "created": 1,
        "model": model, "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
    })
    if include_usage:
        chunks.append({
            "id": "chatcmpl-fixture", "object": "chat.completion.chunk", "created": 1,
            "model": model, "choices": [], "usage": dict(CHAT_USAGE),
        })
    return ("".join("data: " + json.dumps(chunk) + "\n\n" for chunk in chunks) + "data: [DONE]\n\n").encode()


def responses_response(model):
    return {
        "id": "resp-fixture", "object": "response", "created_at": 1,
        "status": "completed", "error": None, "incomplete_details": None,
        "instructions": None, "model": model,
        "output": [{
            "id": "msg-fixture", "type": "message", "role": "assistant", "status": "completed",
            "content": [{"type": "output_text", "text": ANSWER, "annotations": []}],
        }],
        "parallel_tool_calls": True, "tools": [], "tool_choice": "auto",
        "temperature": 1.0, "top_p": 1.0,
        "usage": {
            "input_tokens": 7, "output_tokens": 9, "total_tokens": 16,
            "input_tokens_details": {"cached_tokens": 0},
            "output_tokens_details": {"reasoning_tokens": 0},
        },
    }


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
                if self.path not in ("/openai/v1/chat/completions", "/openai/v1/responses"):
                    self.send_error(404)
                    return
                if body.get("model") == "error-deployment":
                    status, content_type, payload = 401, "application/json", json.dumps(ERROR).encode()
                elif self.path.endswith("/responses"):
                    status, content_type, payload = 200, "application/json", json.dumps(responses_response(body["model"])).encode()
                elif body.get("stream"):
                    include_usage = body.get("stream_options", {}).get("include_usage", False)
                    status, content_type, payload = 200, "text/event-stream", chat_stream(body["model"], include_usage)
                else:
                    status, content_type, payload = 200, "application/json", json.dumps(chat_response(body["model"])).encode()
                self.send_response(status)
                self.send_header("Content-Type", content_type)
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

            def log_message(self, _format, *args):
                return

        self._server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.base_url = f"http://127.0.0.1:{self._server.server_port}/openai/v1/"
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
