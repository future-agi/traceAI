"""A loopback-only, OpenAI-compatible fake chat completions server for tests.

No network and no vendor API: the server binds to 127.0.0.1 on a free port and
answers ``POST /v1/chat/completions`` with deterministic replies chosen from the
request body:

* a group-chat speaker-selection prompt gets ``speaker_reply`` (an agent name);
* a request whose last message is a tool result gets ``final_reply``;
* a request that offers tools gets one tool call to the first offered tool;
* anything else gets ``text_reply``.

Every reply carries ``usage`` so token attributes can be asserted.
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


class FakeOpenAI:
    def __init__(
        self,
        *,
        text_reply: str = "Hello from the fake model. TERMINATE",
        final_reply: str = "Tool says sunny. TERMINATE",
        speaker_reply: str = "critic",
        tool_arguments: Optional[Dict[str, Any]] = None,
    ) -> None:
        self.text_reply = text_reply
        self.final_reply = final_reply
        self.speaker_reply = speaker_reply
        self.tool_arguments = tool_arguments if tool_arguments is not None else {"city": "Paris"}
        self.requests: List[Dict[str, Any]] = []
        self._lock = threading.Lock()
        self._counter = 0
        owner = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self) -> None:  # noqa: N802
                if not self.path.rstrip("/").endswith("/chat/completions"):
                    self.send_error(HTTPStatus.NOT_FOUND)
                    return
                length = int(self.headers.get("Content-Length", "0"))
                body = json.loads(self.rfile.read(length).decode("utf-8") or "{}")
                payload = owner._reply(body)
                data = json.dumps(payload).encode("utf-8")
                self.send_response(HTTPStatus.OK)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
                return

        self._server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.base_url = "http://127.0.0.1:{0}/v1".format(self._server.server_port)
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()

    # Context manager -------------------------------------------------------

    def __enter__(self) -> "FakeOpenAI":
        return self

    def __exit__(self, *_exc: Any) -> None:
        self.close()

    def close(self) -> None:
        self._server.shutdown()
        self._server.server_close()
        self._thread.join()

    # Helpers ---------------------------------------------------------------

    def llm_config(self, **extra: Any) -> Dict[str, Any]:
        entry = {
            "api_type": "openai",
            "model": FAKE_MODEL,
            "api_key": "sk-placeholder-not-a-real-key",
            "base_url": self.base_url,
        }
        entry.update(extra)
        return {"config_list": [entry], "cache_seed": None}

    def _reply(self, body: Dict[str, Any]) -> Dict[str, Any]:
        with self._lock:
            self.requests.append(body)
            self._counter += 1
            n = self._counter
        messages = body.get("messages") or []
        joined = " ".join(str(m.get("content") or "") for m in messages if isinstance(m, dict))
        last = messages[-1] if messages else {}

        message: Dict[str, Any]
        finish = "stop"
        if "select the next role" in joined or "Then select the next role" in joined:
            message = {"role": "assistant", "content": self.speaker_reply}
        elif isinstance(last, dict) and last.get("role") == "tool":
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
