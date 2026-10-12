"""Local Responses fixtures, including Perplexity's search_results output item."""

import copy
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

REQUEST_MODEL = "openai/gpt-5.6-sol"
RESPONSE_MODEL = "openai/gpt-5.6-sol-2026-09-01"  # Synthetic dated id for fixtures only.
ANSWER = "A solar eclipse happens when the Moon blocks the Sun."
DELTAS = ("A solar eclipse ", "happens when the Moon ", "blocks the Sun.")
SEARCH_URL = "https://example.invalid/fixture-search-result"
SEARCH_SNIPPET = "fixture-search-snippet-marker"
QUERY_MARKER = "fixture-prompt-derived-query-marker"
INSTRUCTIONS_MARKER = "fixture-provider-instructions-marker"
USAGE = {
    "input_tokens": 17,
    "output_tokens": 11,
    "total_tokens": 28,
    "cost": {
        "currency": "USD",
        "input_cost": 0.00017,
        "output_cost": 0.00022,
        "total_cost": 0.00248,
        "cache_creation_cost": None,
        "cache_read_cost": 0.00009,
        "tool_calls_cost": 0.002,
    },
    "input_tokens_details": {
        "cache_creation_input_tokens": 0,
        "cache_read_input_tokens": 9,
        "cached_tokens": 9,
    },
    "tool_calls_details": {"search_web": {"invocation": 1}},
    "output_tokens_details": {"reasoning_tokens": 4},
}
ERROR = {"error": {"message": "Invalid API key", "type": "invalid_request_error", "code": 401}}


def response_fixture(model=RESPONSE_MODEL, include_usage=True, include_usage_details=True):
    response = {
        "id": "resp_placeholder",
        "created_at": 1790812800,
        "object": "response",
        "model": model,
        "status": "completed",
        "output": [
            {
                "type": "search_results",
                "results": [{
                    "id": 1,
                    "title": "Fixture source",
                    "url": SEARCH_URL,
                    "snippet": SEARCH_SNIPPET,
                    "date": "2026-10-01",
                    "last_updated": "2026-10-02",
                    "source": "web",
                }, {
                    "id": 2,
                    "title": "Second fixture source",
                    "url": SEARCH_URL + "-second",
                    "snippet": SEARCH_SNIPPET + "-second",
                    "date": None,
                    "last_updated": "2026-10-02",
                    "source": "web",
                }],
                "queries": [QUERY_MARKER + " solar eclipse", "fixture eclipse characteristics"],
            },
            {
                "id": "msg_placeholder",
                "type": "message",
                "role": "assistant",
                "status": "completed",
                "content": [{"type": "output_text", "text": ANSWER, "annotations": [], "logprobs": []}],
            },
        ],
        "error": None,
        "background": False,
        "completed_at": 1790812800,
        "frequency_penalty": 0,
        "incomplete_details": None,
        "instructions": "Placeholder provider system prompt.\n" + INSTRUCTIONS_MARKER,
        "max_output_tokens": 8192,
        "max_tool_calls": None,
        "metadata": {},
        "parallel_tool_calls": True,
    }
    if include_usage:
        response["usage"] = copy.deepcopy(USAGE)
        if not include_usage_details:
            for key in ("input_tokens_details", "output_tokens_details", "tool_calls_details"):
                response["usage"].pop(key)
    return response


def stream_fixture(model=RESPONSE_MODEL, include_usage=True, include_usage_details=True):
    completed = response_fixture(model, include_usage, include_usage_details)
    created = {**completed, "status": "in_progress", "output": [], "usage": None, "completed_at": None}
    events = [{"type": "response.created", "sequence_number": 0, "response": created}]
    for sequence, delta in enumerate(DELTAS, 1):
        events.append({
            "type": "response.output_text.delta",
            "sequence_number": sequence,
            "output_index": 1,
            "content_index": 0,
            "item_id": "msg_placeholder",
            "delta": delta,
        })
    events.append({
        "type": "response.completed",
        "sequence_number": len(DELTAS) + 1,
        "response": completed,
    })
    return ("".join(
        f"event: {event['type']}\ndata: {json.dumps(event)}\n\n" for event in events
    ) + "data: [DONE]\n\n").encode()


class FakeOpenAI:
    """Serve fixture responses on 127.0.0.1 and retain received requests."""

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
                if self.path != "/v1/responses":
                    self.send_error(404)
                    return
                if body.get("input") == "fixture-error":
                    status, content_type, payload = 401, "application/json", json.dumps(ERROR).encode()
                elif body.get("stream"):
                    status, content_type, payload = 200, "text/event-stream", stream_fixture()
                else:
                    status, content_type, payload = 200, "application/json", json.dumps(response_fixture()).encode()
                self.send_response(status)
                self.send_header("Content-Type", content_type)
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

            def log_message(self, _format, *args):
                return

        self._server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.base_url = f"http://127.0.0.1:{self._server.server_port}/v1"
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)

    def __enter__(self):
        self._thread.start()
        return self

    def __exit__(self, *_exc):
        self._server.shutdown()
        self._server.server_close()
        self._thread.join()

    def requests(self):
        with self._lock:
            return copy.deepcopy(self._requests)
