"""A loopback-only, OpenAI-compatible fake chat completions server for tests.

No network and no vendor API: the server binds to 127.0.0.1 on a free port and
answers ``POST /v1/chat/completions`` with deterministic replies chosen from the
request body:

* model ``broken-model`` gets HTTP 400 (the OpenAI client does not retry 400);
* a request whose last message is a tool result gets ``final_reply``;
* a request that offers tools gets one tool call to the first offered tool;
* anything else gets ``text_reply``.

``"stream": true`` is answered as server-sent events, with a final usage chunk
when ``stream_options.include_usage`` is set (Semantic Kernel sets it).
Every reply carries usage so token attributes can be asserted.
"""

from __future__ import annotations

import json
import threading
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Dict, List, Optional

PROMPT_TOKENS = 11
COMPLETION_TOKENS = 7
FAKE_MODEL = "gpt-4o-mini"
BROKEN_MODEL = "broken-model"


class FakeOpenAI:
    def __init__(
        self,
        *,
        text_reply: str = "Hello from the fake model.",
        final_reply: str = "The tool says it is sunny.",
        tool_arguments: Optional[Dict[str, Any]] = None,
    ) -> None:
        self.text_reply = text_reply
        self.final_reply = final_reply
        self.tool_arguments = tool_arguments if tool_arguments is not None else {"city": "Paris"}
        self.requests: List[Dict[str, Any]] = []
        self._lock = threading.Lock()
        self._counter = 0
        owner = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def do_POST(self) -> None:  # noqa: N802
                length = int(self.headers.get("Content-Length", "0"))
                raw = self.rfile.read(length)
                if not self.path.rstrip("/").endswith("/chat/completions"):
                    self.send_error(HTTPStatus.NOT_FOUND)
                    return
                body = json.loads(raw.decode("utf-8") or "{}")
                if body.get("model") == BROKEN_MODEL:
                    owner._record(body)
                    self._send_json(
                        HTTPStatus.BAD_REQUEST,
                        {"error": {"message": "fake model rejected", "type": "invalid_request_error"}},
                    )
                    return
                payload = owner._reply(body)
                if body.get("stream"):
                    self._send_stream(payload, include_usage=bool((body.get("stream_options") or {}).get("include_usage")))
                else:
                    self._send_json(HTTPStatus.OK, payload)

            def _send_json(self, status: HTTPStatus, payload: Dict[str, Any]) -> None:
                data = json.dumps(payload).encode("utf-8")
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def _send_stream(self, payload: Dict[str, Any], include_usage: bool) -> None:
                events = []
                choice = payload["choices"][0]
                message = choice["message"]
                base = {k: payload[k] for k in ("id", "created", "model")}
                base["object"] = "chat.completion.chunk"
                if message.get("tool_calls"):
                    call = message["tool_calls"][0]
                    delta = {"role": "assistant", "tool_calls": [dict(call, index=0)]}
                    events.append(dict(base, choices=[{"index": 0, "delta": delta, "finish_reason": None}]))
                else:
                    text = message.get("content") or ""
                    half = len(text) // 2
                    events.append(dict(base, choices=[{"index": 0, "delta": {"role": "assistant", "content": text[:half]}, "finish_reason": None}]))
                    events.append(dict(base, choices=[{"index": 0, "delta": {"content": text[half:]}, "finish_reason": None}]))
                events.append(dict(base, choices=[{"index": 0, "delta": {}, "finish_reason": choice["finish_reason"]}]))
                if include_usage:
                    events.append(dict(base, choices=[], usage=payload["usage"]))
                data = b"".join(b"data: " + json.dumps(e).encode("utf-8") + b"\n\n" for e in events)
                data += b"data: [DONE]\n\n"
                self.send_response(HTTPStatus.OK)
                self.send_header("Content-Type", "text/event-stream")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
                return

        self._server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.base_url = "http://127.0.0.1:{0}/v1".format(self._server.server_port)
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()

    def __enter__(self) -> "FakeOpenAI":
        return self

    def __exit__(self, *_exc: Any) -> None:
        self.close()

    def close(self) -> None:
        self._server.shutdown()
        self._server.server_close()
        self._thread.join()

    def _record(self, body: Dict[str, Any]) -> int:
        with self._lock:
            self.requests.append(body)
            self._counter += 1
            return self._counter

    def _reply(self, body: Dict[str, Any]) -> Dict[str, Any]:
        n = self._record(body)
        messages = body.get("messages") or []
        last = messages[-1] if messages else {}

        message: Dict[str, Any]
        finish = "stop"
        if isinstance(last, dict) and last.get("role") == "tool":
            message = {"role": "assistant", "content": self.final_reply}
        elif body.get("tools"):
            tool = body["tools"][0]["function"]["name"]
            message = {
                "role": "assistant",
                "content": None,
                "tool_calls": [
                    {
                        "id": "call_fake_{0}".format(n),
                        "type": "function",
                        "function": {"name": tool, "arguments": json.dumps(self.tool_arguments)},
                    }
                ],
            }
            finish = "tool_calls"
        else:
            message = {"role": "assistant", "content": self.text_reply}

        return {
            "id": "chatcmpl-fake-{0}".format(n),
            "object": "chat.completion",
            "created": 1700000000,
            "model": body.get("model") or FAKE_MODEL,
            "choices": [{"index": 0, "message": message, "finish_reason": finish}],
            "usage": {
                "prompt_tokens": PROMPT_TOKENS,
                "completion_tokens": COMPLETION_TOKENS,
                "total_tokens": PROMPT_TOKENS + COMPLETION_TOKENS,
            },
        }
