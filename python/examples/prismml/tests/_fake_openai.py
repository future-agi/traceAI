"""Synthetic OpenAI fixtures; never start Bonsai or download model weights."""

import copy
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

REQUEST_MODEL = "bonsai"
RESPONSE_MODEL = "Ternary-Bonsai-2-27B-Q4_K_M.gguf"
ANSWER = "Hello from the Bonsai fixture."
USAGE = {"prompt_tokens": 13, "completion_tokens": 7, "total_tokens": 20}
ERROR = {"error": {"message": "Synthetic fixture rejection", "type": "authentication_error",
                   "param": None, "code": "fixture_rejected"}}
TOOLS = [{"type": "function", "function": {
    "name": "get_weather", "description": "Get the weather for a city.",
    "parameters": {"type": "object", "properties": {"city": {"type": "string"}},
                   "required": ["city"]},
}}]
TOOL_CALL = {"id": "call-fixture-weather", "type": "function", "function": {
    "name": "get_weather", "arguments": '{"city":"Paris"}',
}}


def chat_response(include_usage=True, tool_call=False):
    message = {"role": "assistant", "content": None if tool_call else ANSWER}
    if tool_call:
        message["tool_calls"] = [copy.deepcopy(TOOL_CALL)]
    result = {
        "id": "chatcmpl-fixture", "object": "chat.completion", "created": 1,
        "model": RESPONSE_MODEL,
        "choices": [{"index": 0, "message": message,
                     "finish_reason": "tool_calls" if tool_call else "stop"}],
    }
    if include_usage:
        result["usage"] = USAGE.copy()
    return result


def stream_body(include_usage=False, text=ANSWER):
    common = {"id": "chatcmpl-stream-fixture", "object": "chat.completion.chunk",
              "created": 1, "model": RESPONSE_MODEL}
    chunks = []
    for part in (text[:len(text) // 2], text[len(text) // 2:]):
        chunks.append({**common, "choices": [{"index": 0,
                       "delta": {"role": "assistant", "content": part},
                       "finish_reason": None}]})
    chunks.append({**common, "choices": [{"index": 0, "delta": {},
                                          "finish_reason": "stop"}]})
    if include_usage:
        chunks.append({**common, "choices": [], "usage": USAGE.copy()})
    return ("".join(f"data: {json.dumps(chunk)}\n\n" for chunk in chunks)
            + "data: [DONE]\n\n").encode()


class FakeOpenAI:
    """Threaded loopback server with chat, SSE and a synthetic error response."""

    def __init__(self):
        self._requests = []
        self._lock = threading.Lock()
        owner = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                with owner._lock:
                    owner._requests.append({"path": self.path, "body": body,
                        "headers": {key.lower(): value for key, value in self.headers.items()}})
                status, content_type = 200, "application/json"
                if self.path != "/v1/chat/completions":
                    status, data = 404, json.dumps(ERROR).encode()
                elif body["messages"][0]["content"] == "fixture-error":
                    status, data = 401, json.dumps(ERROR).encode()
                elif body.get("stream"):
                    content_type = "text/event-stream"
                    data = stream_body(body.get("stream_options", {}).get("include_usage", False))
                else:
                    data = json.dumps(chat_response()).encode()
                self.send_response(status)
                self.send_header("Content-Type", content_type)
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def log_message(self, _format, *args):
                pass

        self._server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.origin = f"http://127.0.0.1:{self._server.server_port}"
        self.base_url = self.origin + "/v1"
        self._thread = threading.Thread(
            target=lambda: self._server.serve_forever(poll_interval=0.01), daemon=True)
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
