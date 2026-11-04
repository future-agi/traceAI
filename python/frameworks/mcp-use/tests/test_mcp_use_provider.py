"""Real provider normalization and MCPAgent, with only loopback HTTP/SSE IO."""

from __future__ import annotations

import asyncio
import json
import threading
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest
from _mcp_use_support import drive, mcp_client, new_provider, wire
from fi_instrumentation import TraceConfig
from langchain_anthropic import ChatAnthropic
from mcp_use import MCPAgent

from traceai_mcp_use import FutureAGICallback

MODEL = "claude-offline-synthetic-model"
USAGE = {
    "input_tokens": 12,
    "output_tokens": 3,
    "cache_read_input_tokens": 10,
    "cache_creation_input_tokens": 50,
    "cache_creation": {"ephemeral_5m_input_tokens": 30, "ephemeral_1h_input_tokens": 20},
}


@contextmanager
def provider_server():
    """Exercise the SDK's HTTP and SSE parsers, not a handcrafted AIMessage."""
    requests = []

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_POST(self):
            request = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            requests.append(request)
            message = {
                "id": "msg_offline",
                "type": "message",
                "role": "assistant",
                "model": request["model"],
                "content": [{"type": "text", "text": "offline cached answer"}],
                "stop_reason": "end_turn",
                "stop_sequence": None,
                "usage": USAGE,
            }
            if request.get("stream"):
                events = [
                    ("message_start", {
                        "type": "message_start",
                        "message": dict(message, content=[], stop_reason=None, usage=dict(USAGE, output_tokens=0)),
                    }),
                    ("content_block_start", {
                        "type": "content_block_start", "index": 0,
                        "content_block": {"type": "text", "text": ""},
                    }),
                    ("content_block_delta", {
                        "type": "content_block_delta", "index": 0,
                        "delta": {"type": "text_delta", "text": "offline cached answer"},
                    }),
                    ("content_block_stop", {"type": "content_block_stop", "index": 0}),
                    ("message_delta", {
                        "type": "message_delta",
                        "delta": {"stop_reason": "end_turn", "stop_sequence": None},
                        "usage": USAGE,
                    }),
                    ("message_stop", {"type": "message_stop"}),
                ]
                body = "".join(
                    "event: " + name + "\ndata: " + json.dumps(data) + "\n\n"
                    for name, data in events
                ).encode()
                content_type = "text/event-stream"
            else:
                body = json.dumps(message).encode()
                content_type = "application/json"
            self.send_response(200)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield "http://127.0.0.1:" + str(server.server_port), requests
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=3)


def provider_spans(method, hidden=False, model=MODEL):
    exporter, provider = new_provider()
    handler = FutureAGICallback(
        tracer_provider=provider,
        config=TraceConfig(hide_inputs=hidden, hide_outputs=hidden),
    )
    try:
        with provider_server() as (origin, requests):
            llm = ChatAnthropic(
                model=model, api_key="synthetic-placeholder-key", base_url=origin,
                max_tokens=32, max_retries=0, timeout=10,
                streaming=method != "run",
            )

            async def run():
                client = mcp_client()
                try:
                    agent = MCPAgent(llm=llm, client=client, callbacks=[handler])
                    await drive(agent, method, query="show usage")
                finally:
                    await client.close_all_sessions()

            asyncio.run(run())
            # Assert that both transports really run, not just the async APIs.
            assert len(requests) == 1
            assert bool(requests[0].get("stream")) == (method != "run")
        assert not handler._runs and not handler._roots
        spans = exporter.get_finished_spans()
        assert len(spans) == 2
        return spans
    finally:
        provider.shutdown()


@pytest.mark.parametrize("method", ["run", "stream", "stream_events"])
@pytest.mark.parametrize("hidden", [False, True])
def test_real_provider_cache_tiers(method, hidden):
    spans = provider_spans(method, hidden)
    llm, = [s for s in spans if s.attributes.get("gen_ai.span.kind") == "LLM"]
    attrs = llm.attributes
    # LangChain already includes cache reads and writes in input/total counts.
    assert attrs["gen_ai.usage.input_tokens"] == 72
    assert attrs["gen_ai.usage.output_tokens"] == 3
    assert attrs["gen_ai.usage.total_tokens"] == 75
    assert attrs["gen_ai.usage.cache_read_tokens"] == 10
    assert attrs["gen_ai.usage.cache_write_tokens"] == 50
    # Keep the different billing tiers, not just the aggregate write count.
    assert attrs["mcp_use.llm.cache_creation.ephemeral_5m_input_tokens"] == 30
    assert attrs["mcp_use.llm.cache_creation.ephemeral_1h_input_tokens"] == 20
    assert attrs["gen_ai.request.model"] == attrs["gen_ai.response.model"] == MODEL
    assert llm.name == "chat " + MODEL


@pytest.mark.parametrize("method", ["run", "stream", "stream_events"])
@pytest.mark.parametrize("home", ["/work/synthetic-review-user", "/data/synthetic-review-user"])
@pytest.mark.parametrize("hidden", [False, True])
def test_local_model_identifiers_never_export_home(monkeypatch, method, home, hidden):
    monkeypatch.setenv("HOME", home)
    spans = provider_spans(method, hidden, model=home + "/models/synthetic-model.gguf")
    assert home not in wire(spans)
    llm, = [s for s in spans if s.attributes.get("gen_ai.span.kind") == "LLM"]
    assert llm.name == "chat [path]"
    assert llm.attributes["gen_ai.request.model"] == "[path]"
    assert llm.attributes["gen_ai.response.model"] == "[path]"
