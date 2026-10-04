"""A loopback stand-in for the OpenAI Chat Completions streaming API.

A request that offers tools and has no tool result yet gets a get_weather tool
call. Any other request gets a text answer. Both responses end with an OpenAI
include_usage chunk, so the adapter reports token usage. With ``stall=True``
the text answer stops after its first delta until the server exits, so a
client can abort mid-stream. With ``fail_status=500`` every request gets that
HTTP status and an OpenAI-shaped error body whose message is ``boom``.
"""

from __future__ import annotations

import json
import threading
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

RESPONSE_MODEL = "gpt-4o-mini-2024-07-18"

# Usage per model call. The second call also reports cached and reasoning
# tokens so the dotted gen_ai.usage.* keys are exercised.
TOOL_CALL_USAGE = {"prompt_tokens": 11, "completion_tokens": 7, "total_tokens": 18}
ANSWER_USAGE = {
    "prompt_tokens": 23,
    "completion_tokens": 5,
    "total_tokens": 28,
    "prompt_tokens_details": {"cached_tokens": 3},
    "completion_tokens_details": {"reasoning_tokens": 2},
}


def _chunk(delta: dict[str, Any] | None, finish_reason: str | None = None,
           usage: dict[str, Any] | None = None) -> bytes:
    body: dict[str, Any] = {
        "id": "chatcmpl-fake",
        "object": "chat.completion.chunk",
        "created": 1_700_000_000,
        "model": RESPONSE_MODEL,
        "choices": [] if delta is None else [
            {"index": 0, "delta": delta, "finish_reason": finish_reason}
        ],
    }
    if usage is not None:
        body["usage"] = usage
    return b"data: " + json.dumps(body).encode("utf-8") + b"\n\n"


class FakeOpenAI:
    """Serve /v1/chat/completions on 127.0.0.1 and record each request body."""

    def __init__(self, tool_city: str, answer_text: str, stall: bool = False,
                 fail_status: int | None = None) -> None:
        self.requests: list[dict[str, Any]] = []
        self.authorizations: list[str | None] = []
        self._release = threading.Event()
        lock = threading.Lock()
        owner = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self) -> None:  # noqa: N802
                if self.path.rstrip("/") != "/v1/chat/completions":
                    self.send_error(HTTPStatus.NOT_FOUND)
                    return
                length = int(self.headers.get("Content-Length", "0"))
                request = json.loads(self.rfile.read(length).decode("utf-8"))
                with lock:
                    owner.requests.append(request)
                    owner.authorizations.append(self.headers.get("Authorization"))

                if fail_status is not None:
                    body = json.dumps(
                        {"error": {"message": "boom", "type": "server_error"}}
                    ).encode("utf-8")
                    self.send_response(fail_status)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)
                    return

                has_tool_result = any(
                    message.get("role") == "tool" for message in request.get("messages", [])
                )
                if has_tool_result or not request.get("tools"):
                    events = [
                        _chunk({"role": "assistant", "content": ""}),
                        _chunk({"content": answer_text}),
                        _chunk({}, finish_reason="stop"),
                        _chunk(None, usage=ANSWER_USAGE),
                    ]
                else:
                    arguments = json.dumps({"city": tool_city})
                    events = [
                        _chunk({
                            "role": "assistant",
                            "content": None,
                            "tool_calls": [{
                                "index": 0,
                                "id": "call_fake_1",
                                "type": "function",
                                "function": {"name": "get_weather", "arguments": ""},
                            }],
                        }),
                        _chunk({"tool_calls": [{"index": 0, "function": {"arguments": arguments}}]}),
                        _chunk({}, finish_reason="tool_calls"),
                        _chunk(None, usage=TOOL_CALL_USAGE),
                    ]
                events.append(b"data: [DONE]\n\n")
                if not stall:
                    body = b"".join(events)
                    self.send_response(HTTPStatus.OK)
                    self.send_header("Content-Type", "text/event-stream")
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)
                    return
                # Stalled stream: send the first text delta, then hold the
                # rest until the test exits, so the client can abort mid-stream.
                # HTTP/1.0 without Content-Length: the body ends at close.
                self.send_response(HTTPStatus.OK)
                self.send_header("Content-Type", "text/event-stream")
                self.end_headers()
                try:
                    self.wfile.write(b"".join(events[:2]))
                    self.wfile.flush()
                    owner._release.wait(timeout=10)
                    self.wfile.write(b"".join(events[2:]))
                except (BrokenPipeError, ConnectionResetError):
                    pass

            def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
                return

        self._server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.base_url = "http://127.0.0.1:{0}/v1".format(self._server.server_port)
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()

    def __enter__(self) -> "FakeOpenAI":
        return self

    def __exit__(self, *_exc: Any) -> None:
        self._release.set()
        self._server.shutdown()
        self._server.server_close()
        self._thread.join()
