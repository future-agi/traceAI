"""A loopback stand-in for the OpenAI Chat Completions API (non-streaming).

Every POST to /v1/chat/completions gets one assistant message whose text is
``answer_text``, with fixed token usage, so the instrumentor records model
and usage attributes. With ``tool_call`` the message is that one tool call
instead of text; with ``error`` every request gets that status and JSON body.
Each request body and Authorization header is kept for assertions. Listens
on 127.0.0.1 only.
"""

from __future__ import annotations

import json
import threading
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Optional

RESPONSE_MODEL = "gpt-4o-mini-2024-07-18"
USAGE = {"prompt_tokens": 11, "completion_tokens": 7, "total_tokens": 18}


class FakeOpenAI:
    """Serve /v1/chat/completions on 127.0.0.1 and record each request.

    ``tool_call`` is ``{"id": ..., "name": ..., "arguments": <JSON string>}``;
    ``error`` is ``[status, body]``.
    """

    def __init__(
        self,
        answer_text: str,
        tool_call: Optional[dict[str, str]] = None,
        error: Optional[tuple[int, dict[str, Any]]] = None,
    ) -> None:
        self.requests: list[dict[str, Any]] = []
        self.authorizations: list[str | None] = []
        lock = threading.Lock()
        owner = self

        if tool_call is not None:
            message: dict[str, Any] = {
                "role": "assistant",
                "content": None,
                "tool_calls": [
                    {
                        "id": tool_call["id"],
                        "type": "function",
                        "function": {
                            "name": tool_call["name"],
                            "arguments": tool_call["arguments"],
                        },
                    }
                ],
            }
            finish_reason = "tool_calls"
        else:
            message = {"role": "assistant", "content": answer_text}
            finish_reason = "stop"

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
                if self.path.rstrip("/") != "/v1/chat/completions":
                    self.send_error(HTTPStatus.NOT_FOUND)
                    return
                length = int(self.headers.get("Content-Length", "0"))
                request = json.loads(self.rfile.read(length).decode("utf-8"))
                with lock:
                    owner.requests.append(request)
                    owner.authorizations.append(self.headers.get("Authorization"))
                if error is not None:
                    status, reply = HTTPStatus(error[0]), error[1]
                else:
                    status = HTTPStatus.OK
                    reply = {
                        "id": "chatcmpl-fake",
                        "object": "chat.completion",
                        "created": 1_700_000_000,
                        "model": RESPONSE_MODEL,
                        "choices": [
                            {"index": 0, "message": message, "finish_reason": finish_reason}
                        ],
                        "usage": USAGE,
                    }
                body = json.dumps(reply).encode("utf-8")
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
                return

        self._server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.base_url = "http://127.0.0.1:{0}/v1".format(self._server.server_port)
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()

    def __enter__(self) -> "FakeOpenAI":
        return self

    def __exit__(self, *_exc: Any) -> None:
        self._server.shutdown()
        self._server.server_close()
        self._thread.join()
