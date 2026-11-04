"""Loopback fake of the OpenAI chat-completions and embeddings APIs for EverOS.

Answers every request EverOS 1.4.1 (through everalgo) makes in one
add / flush / search run with a value that parses on the first attempt, so
each EverOS LLM call is one HTTP request: memcell boundary detection, episode
extraction, and the background atomic-fact extraction. Anything else gets
``{}`` (or, for a JSON-schema response format, an instance built from the
schema). Embeddings are deterministic 1024-dimension vectors, the width
EverOS's LanceDB tables are created with.

``malformed_atomic_facts=True`` answers the atomic-fact prompt with JSON that
everalgo rejects, quoting ``LLM_ERROR_MARKER``, for the test that shows what
an extraction error exports with content capture off.

``reject_embedding_of=<text>`` answers any embeddings request whose input
contains ``<text>`` with HTTP 400 and an OpenAI-style error body that quotes
the rejected input, as some gateways do, followed by ``ERROR_TAIL_MARKER`` at
character ``ERROR_TAIL_AT`` of the error message, past EverOS's
4096-character content cap. Other embeddings requests are answered normally.
"""

from __future__ import annotations

import hashlib
import json
import threading
import time
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Optional

# The episode text the fake "extracts"; with capture on it is exported.
EPISODE_MARKER = "EMARK-everos-7c1e"
EPISODE_TEXT = "Ada works on the Lighthouse project. " + EPISODE_MARKER
LLM_ERROR_MARKER = "LMARK-everos-4b2d"
# Ends the rejected-embedding error message, starting at this character.
ERROR_TAIL_MARKER = "TMARK-everos-0e7a"
ERROR_TAIL_AT = 4200
EMBEDDING_DIMENSIONS = 1024
USAGE = {"prompt_tokens": 11, "completion_tokens": 7, "total_tokens": 18}
EMBEDDING_USAGE = {"prompt_tokens": 3, "total_tokens": 3}


def _resolve(schema: dict, root: dict) -> dict:
    ref = schema.get("$ref")
    if ref and ref.startswith("#/"):
        node: Any = root
        for part in ref[2:].split("/"):
            node = node[part]
        return _resolve(node, root)
    return schema


def _instance(schema: dict, root: dict, name: str = "value") -> Any:
    schema = _resolve(schema, root)
    if "const" in schema:
        return schema["const"]
    if "enum" in schema:
        return schema["enum"][0]
    for key in ("anyOf", "oneOf"):
        if key in schema:
            options = [s for s in schema[key] if _resolve(s, root).get("type") != "null"]
            return _instance((options or schema[key])[0], root, name)
    if "allOf" in schema:
        return _instance(schema["allOf"][0], root, name)
    kind = schema.get("type")
    if isinstance(kind, list):
        kind = next((k for k in kind if k != "null"), "null")
    if kind == "object" or "properties" in schema:
        return {
            key: _instance(value, root, key)
            for key, value in schema.get("properties", {}).items()
        }
    if kind == "array":
        return [_instance(schema.get("items", {}), root, name)]
    if kind == "integer":
        return 1
    if kind == "number":
        return 1.0
    if kind == "boolean":
        return True
    if kind == "null":
        return None
    return "fake {0}".format(name)


class FakeOpenAI:
    """Serves /v1/chat/completions and /v1/embeddings on 127.0.0.1 only."""

    def __init__(
        self, malformed_atomic_facts: bool = False, reject_embedding_of: Optional[str] = None
    ) -> None:
        self.malformed_atomic_facts = malformed_atomic_facts
        self.reject_embedding_of = reject_embedding_of
        self._lock = threading.Lock()
        self._requests: list[dict[str, Any]] = []
        owner = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self) -> None:  # noqa: N802
                length = int(self.headers.get("Content-Length", "0"))
                body = json.loads(self.rfile.read(length) or b"{}")
                with owner._lock:
                    owner._requests.append(
                        {
                            "path": self.path,
                            "authorization": self.headers.get("Authorization"),
                            "body": body,
                        }
                    )
                if self.path.endswith("/chat/completions"):
                    status, payload = HTTPStatus.OK, owner._chat(body)
                elif self.path.endswith("/embeddings"):
                    status, payload = owner._embeddings(body)
                else:
                    self.send_error(HTTPStatus.NOT_FOUND)
                    return
                data = json.dumps(payload).encode("utf-8")
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def log_message(self, _format: str, *args: Any) -> None:
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

    def requests(self) -> list[dict[str, Any]]:
        with self._lock:
            return json.loads(json.dumps(self._requests))

    def _answer(self, prompt: str) -> Any:
        # Prompts are everalgo's, recognised by a phrase each one contains.
        if "episode boundaries" in prompt:
            return {"reasoning": "One topic.", "boundaries": [], "should_wait": False}
        if "atomic_fact" in prompt:
            if self.malformed_atomic_facts:
                return {"facts": LLM_ERROR_MARKER}
            return {"atomic_facts": {"time": "2026-10-05", "atomic_fact": ["Ada works on Lighthouse."]}}
        if "(title and content)" in prompt:
            return {"title": "Ada and Lighthouse", "content": EPISODE_TEXT}
        return {}

    def _chat(self, body: dict) -> dict:
        response_format = body.get("response_format") or {}
        if response_format.get("type") == "json_schema":
            schema = response_format.get("json_schema", {}).get("schema", {})
            content = json.dumps(_instance(schema, schema))
        else:
            content = json.dumps(self._answer(json.dumps(body.get("messages", []))))
        return {
            "id": "chatcmpl-fake",
            "object": "chat.completion",
            "created": int(time.time()),
            "model": body.get("model", "gpt-4o-mini"),
            "choices": [
                {
                    "index": 0,
                    "message": {"role": "assistant", "content": content},
                    "finish_reason": "stop",
                }
            ],
            "usage": USAGE,
        }

    def _embeddings(self, body: dict) -> tuple[HTTPStatus, dict]:
        inputs = body.get("input", [])
        if isinstance(inputs, str):
            inputs = [inputs]
        if self.reject_embedding_of and any(self.reject_embedding_of in str(t) for t in inputs):
            message = "invalid input: {0}".format(json.dumps(inputs)).ljust(ERROR_TAIL_AT, ".")
            error = {
                "message": message + ERROR_TAIL_MARKER,
                "type": "invalid_request_error",
                "param": "input",
                "code": None,
            }
            return HTTPStatus.BAD_REQUEST, {"error": error}
        size = int(body.get("dimensions") or EMBEDDING_DIMENSIONS)
        data = []
        for index, text in enumerate(inputs):
            digest = hashlib.sha256(str(text).encode("utf-8")).digest()
            vector = [((digest[i % len(digest)] / 255.0) - 0.5) for i in range(size)]
            data.append({"object": "embedding", "index": index, "embedding": vector})
        return HTTPStatus.OK, {
            "object": "list",
            "data": data,
            "model": body.get("model", "text-embedding-3-small"),
            "usage": EMBEDDING_USAGE,
        }
