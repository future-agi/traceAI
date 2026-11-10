"""A loopback stand-in for the OpenAI Chat Completions API (non-streaming).

Every POST to /v1/chat/completions gets one assistant message whose text is
``answer_text``, with fixed token usage, so the instrumentor records model
and usage attributes. Each request body and Authorization header is kept for
assertions. Listens on 127.0.0.1 only.
"""

from __future__ import annotations

import json
import threading
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

RESPONSE_MODEL = "gpt-4o-mini-2024-07-18"
USAGE = {"prompt_tokens": 11, "completion_tokens": 7, "total_tokens": 18}


class FakeOpenAI:
    """Serve /v1/chat/completions on 127.0.0.1 and record each request."""

    def __init__(self, answer_text: str) -> None:
        self.requests: list[dict[str, Any]] = []
        self.authorizations: list[str | None] = []
        lock = threading.Lock()
        owner = self

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
                body = json.dumps(
                    {
                        "id": "chatcmpl-fake",
                        "object": "chat.completion",
                        "created": 1_700_000_000,
                        "model": RESPONSE_MODEL,
                        "choices": [
                            {
                                "index": 0,
                                "message": {"role": "assistant", "content": answer_text},
                                "finish_reason": "stop",
                            }
                        ],
                        "usage": USAGE,
                    }
                ).encode("utf-8")
                self.send_response(HTTPStatus.OK)
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
