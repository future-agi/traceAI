"""Fixtures for the LlamaIndex instrumentation tests.

``openai_base_url`` serves a minimal OpenAI-compatible chat completions API on
localhost, so the real ``llama_index.llms.openai.OpenAI`` class runs its normal
HTTP and streaming code paths with no network access or API key.
"""
from __future__ import annotations

import contextlib
import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Dict, Iterator, List

import pytest
from opentelemetry.sdk.trace import ReadableSpan, TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

ANSWER = "The answer is 6."
ANSWER_CHUNKS = ["The ", "answer ", "is ", "6."]
FAILING_MODEL = "gpt-3.5-turbo"
FAILING_MODEL_ERROR = "model is unavailable"
TOOL_ARGUMENTS = {"a": 2, "b": 3}
USAGE = {"prompt_tokens": 11, "completion_tokens": 7, "total_tokens": 18}


class _FakeOpenAIHandler(BaseHTTPRequestHandler):
    def log_message(self, *args: Any) -> None:
        pass

    def _send_json(self, status: int, body: Dict[str, Any]) -> None:
        payload = json.dumps(body).encode()
        self.send_response(status)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def _send_event(self, body: Dict[str, Any]) -> None:
        self.wfile.write(b"data: " + json.dumps(body).encode() + b"\n\n")
        self.wfile.flush()

    def do_POST(self) -> None:
        length = int(self.headers.get("content-length", 0))
        request = json.loads(self.rfile.read(length) or b"{}")
        if not self.path.endswith("/chat/completions"):
            self._send_json(404, {"error": {"message": "not found"}})
            return
        if request["model"] == FAILING_MODEL:
            error = {"message": FAILING_MODEL_ERROR, "type": "invalid_request_error"}
            self._send_json(400, {"error": error})
            return

        wants_tool_call = request.get("tools") and not any(
            message["role"] == "tool" for message in request["messages"]
        )
        if wants_tool_call:
            tool_call = {
                "id": "call_1",
                "type": "function",
                "function": {
                    "name": request["tools"][0]["function"]["name"],
                    "arguments": json.dumps(TOOL_ARGUMENTS),
                },
            }
            message: Dict[str, Any] = {
                "role": "assistant",
                "content": None,
                "tool_calls": [tool_call],
            }
            finish_reason = "tool_calls"
        else:
            message = {"role": "assistant", "content": ANSWER}
            finish_reason = "stop"
        base = {"id": "chatcmpl-1", "created": int(time.time()), "model": request["model"]}

        if not request.get("stream"):
            choice = {"index": 0, "message": message, "finish_reason": finish_reason}
            self._send_json(
                200,
                {**base, "object": "chat.completion", "choices": [choice], "usage": USAGE},
            )
            return

        self.send_response(200)
        self.send_header("content-type", "text/event-stream")
        self.end_headers()
        chunk = {**base, "object": "chat.completion.chunk"}
        deltas: List[Dict[str, Any]]
        if finish_reason == "tool_calls":
            tool_delta = {"index": 0, **message["tool_calls"][0]}
            deltas = [{"role": "assistant", "tool_calls": [tool_delta]}]
        else:
            deltas = [{"role": "assistant", "content": ANSWER_CHUNKS[0]}]
            deltas += [{"content": text} for text in ANSWER_CHUNKS[1:]]
        for delta in deltas:
            choice = {"index": 0, "delta": delta, "finish_reason": None}
            self._send_event({**chunk, "choices": [choice]})
        choice = {"index": 0, "delta": {}, "finish_reason": finish_reason}
        self._send_event({**chunk, "choices": [choice]})
        self.wfile.write(b"data: [DONE]\n\n")


@pytest.fixture(scope="session")
def openai_base_url() -> Iterator[str]:
    server = ThreadingHTTPServer(("127.0.0.1", 0), _FakeOpenAIHandler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}/v1"
    finally:
        server.shutdown()


@pytest.fixture
def exporter() -> Iterator[InMemorySpanExporter]:
    """Instrument LlamaIndex against an isolated in-memory exporter."""
    from traceai_llamaindex import LlamaIndexInstrumentor

    span_exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(span_exporter))
    instrumentor = LlamaIndexInstrumentor()
    instrumentor.instrument(tracer_provider=provider)
    try:
        yield span_exporter
    finally:
        with contextlib.suppress(Exception):
            instrumentor.uninstrument()


def wait_for_span(
    exporter: InMemorySpanExporter, name: str, timeout: float = 5.0
) -> ReadableSpan:
    """Return the first finished span called ``name``; streamed spans end asynchronously."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        for span in exporter.get_finished_spans():
            if span.name == name:
                return span
        time.sleep(0.05)
    finished = sorted({span.name for span in exporter.get_finished_spans()})
    raise AssertionError(f"span {name!r} never finished; finished spans: {finished}")
