"""Loopback fake of the OpenAI chat-completions and embeddings APIs.

Answers every request Cognee 1.6.2 makes in one add/cognify/search run with
a value that validates on the first attempt, so each Cognee LLM call is one
HTTP request: Cognee's json_object fallback never runs. Schemas it does not
know get an instance built from the JSON schema itself.
"""

from __future__ import annotations

import hashlib
import json
import threading
import time
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

ANSWER_MARKER = "COGNEE-ANSWER-MARKER-5d1e"


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


def _knowledge_graph() -> dict:
    return {
        "nodes": [
            {"id": "ada", "name": "Ada", "type": "Person", "description": "A fictional engineer."},
            {"id": "lighthouse", "name": "Lighthouse", "type": "Project", "description": "A fictional project."},
        ],
        "edges": [
            {
                "source_node_id": "ada",
                "target_node_id": "lighthouse",
                "relationship_name": "works_on",
                "description": "Ada works on Lighthouse.",
            }
        ],
    }


def _session_turn_analysis() -> dict:
    # Every field null or empty: "no routing signal", so search goes on to
    # answer the question. A schema-derived instance fails Cognee's
    # validation and triggers a second (json_object) request.
    return {
        "response_to_user": None,
        "query_to_answer": None,
        "served_context_ratings": [],
        "candidate_context_updates": [],
        "previous_answer_rating": None,
    }


class FakeOpenAI:
    """Serves /v1/chat/completions and /v1/embeddings on 127.0.0.1 only."""

    def __init__(self, dimensions: int = 16) -> None:
        self.dimensions = dimensions
        self._lock = threading.Lock()
        self._requests: list[dict[str, Any]] = []
        owner = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self) -> None:  # noqa: N802
                length = int(self.headers.get("Content-Length", "0"))
                body = json.loads(self.rfile.read(length) or b"{}")
                with owner._lock:
                    owner._requests.append({"path": self.path, "body": body})
                if self.path.endswith("/chat/completions"):
                    payload = owner._chat(body)
                elif self.path.endswith("/embeddings"):
                    payload = owner._embeddings(body)
                else:
                    self.send_error(HTTPStatus.NOT_FOUND)
                    return
                data = json.dumps(payload).encode("utf-8")
                self.send_response(HTTPStatus.OK)
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

    def close(self) -> None:
        self._server.shutdown()
        self._server.server_close()
        self._thread.join()

    def requests(self) -> list[dict[str, Any]]:
        with self._lock:
            return json.loads(json.dumps(self._requests))

    def _chat(self, body: dict) -> dict:
        response_format = body.get("response_format") or {}
        if response_format.get("type") == "json_schema":
            spec = response_format.get("json_schema", {})
            schema = spec.get("schema", {})
            if spec.get("name") == "KnowledgeGraph":
                content = json.dumps(_knowledge_graph())
            elif spec.get("name") == "SessionTurnAnalysis":
                content = json.dumps(_session_turn_analysis())
            else:
                content = json.dumps(_instance(schema, schema))
        elif response_format.get("type") == "json_object":
            content = json.dumps({"answer": ANSWER_MARKER})
        else:
            content = "Ada works on Lighthouse. {0}".format(ANSWER_MARKER)
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
            "usage": {"prompt_tokens": 11, "completion_tokens": 7, "total_tokens": 18},
        }

    def _embeddings(self, body: dict) -> dict:
        inputs = body.get("input", [])
        if isinstance(inputs, str):
            inputs = [inputs]
        size = int(body.get("dimensions") or self.dimensions)
        data = []
        for index, text in enumerate(inputs):
            digest = hashlib.sha256(str(text).encode("utf-8")).digest()
            vector = [((digest[i % len(digest)] / 255.0) - 0.5) for i in range(size)]
            data.append({"object": "embedding", "index": index, "embedding": vector})
        return {
            "object": "list",
            "data": data,
            "model": body.get("model", "text-embedding-3-small"),
            "usage": {"prompt_tokens": 3, "total_tokens": 3},
        }
