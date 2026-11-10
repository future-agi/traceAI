"""Real SDK HTTP/SSE responses retain operational metadata under attribute pressure."""

import asyncio
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from opentelemetry.trace import StatusCode

from traceai_mistralai import MistralAIInstrumentor
from test_sdk import Mistral, NATIVE, httpx


@pytest.fixture
def tool_server():
    calls = []

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass

        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            calls.append(body)
            streaming = body.get("stream", False)
            tools = body.get("tools", [])
            message = {"role": "assistant", "content": "synthetic-output"}
            if tools:
                message["tool_calls"] = [
                    {
                        "id": f"call-{index}",
                        "type": "function",
                        "function": {
                            "name": tool["function"]["name"],
                            "arguments": '{"x":1}',
                        },
                        **({"index": index} if streaming else {}),
                    }
                    for index, tool in enumerate(tools)
                ]
            response = {
                "id": "synthetic-id",
                "object": "chat.completion.chunk" if streaming else "chat.completion",
                "created": 1,
                "model": "resolved-model",
                "choices": [{
                    "index": 0,
                    "delta" if streaming else "message": message,
                    "finish_reason": "tool_calls" if tools else "stop",
                }],
                "usage": {"prompt_tokens": 13, "completion_tokens": 17, "total_tokens": 30},
            }
            self.send_response(200)
            self.send_header(
                "Content-Type", "text/event-stream" if streaming else "application/json"
            )
            self.end_headers()
            data = json.dumps(response)
            self.wfile.write(
                (f"data: {data}\n\ndata: [DONE]\n\n" if streaming else data).encode()
            )

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}", calls
    finally:
        server.shutdown()
        thread.join()
        server.server_close()


@pytest.mark.parametrize("async_client", [False, True])
@pytest.mark.parametrize("streaming", [False, True])
@pytest.mark.parametrize("tool_count", [0, 64])
def test_response_metadata_survives_large_tool_responses(
    tool_server, async_client, streaming, tool_count
):
    url, calls = tool_server
    exporter = InMemorySpanExporter()
    # Do not raise OTel's default 128-attribute limit to make this pass.
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    instrumentor = MistralAIInstrumentor()
    params = {
        "model": "requested-alias",
        "messages": [{"role": "user", "content": "synthetic-input"}],
    }
    if tool_count:
        params["tools"] = [
            {
                "type": "function",
                "function": {
                    "name": f"lookup_{index}",
                    "parameters": {"type": "object", "properties": {"x": {"type": "integer"}}},
                },
            }
            for index in range(tool_count)
        ]

    def assert_response(response):
        assert response.model == "resolved-model"
        message = response.choices[0].delta if streaming else response.choices[0].message
        assert message.content == "synthetic-output"
        assert len(message.tool_calls or []) == tool_count
        assert response.usage.total_tokens == 30

    async def call_async():
        async with httpx.AsyncClient() as transport:
            client = Mistral(
                api_key="synthetic-key", server_url=url, async_client=transport
            )
            if streaming:
                async with await client.chat.stream_async(**params) as stream:
                    chunks = [chunk async for chunk in stream]
                    assert len(chunks) == 1
                    assert_response(chunks[0].data)
            else:
                assert_response(await client.chat.complete_async(**params))

    try:
        instrumentor.instrument(tracer_provider=provider)
        with provider.get_tracer("test").start_as_current_span("parent") as parent:
            if async_client:
                asyncio.run(call_async())
            else:
                with httpx.Client() as transport:
                    client = Mistral(api_key="synthetic-key", server_url=url, client=transport)
                    if streaming:
                        with client.chat.stream(**params) as stream:
                            chunks = list(stream)
                            assert len(chunks) == 1
                            assert_response(chunks[0].data)
                    else:
                        assert_response(client.chat.complete(**params))
        assert len(calls) == 1
        assert calls[0]["model"] == "requested-alias"
        assert len(calls[0].get("tools", [])) == tool_count
        spans = exporter.get_finished_spans()
        assert len(spans) == 2
        child = spans[0]
        assert child.parent.span_id == parent.get_span_context().span_id
        assert child.status.status_code == StatusCode.OK
        attrs = dict(child.attributes)
        expected = {
            "gen_ai.request.model": "requested-alias",
            "gen_ai.provider.name": "mistral_ai" if NATIVE else "mistralai",
            "gen_ai.response.model": "resolved-model",
            "gen_ai.usage.input_tokens": 13,
            "gen_ai.usage.output_tokens": 17,
            "gen_ai.span.kind": "LLM",
        }
        # Native GenAI semantics expose input/output counts, not a total field.
        if not NATIVE:
            expected["gen_ai.usage.total_tokens"] = 30
        assert {key: attrs.get(key) for key in expected} == expected
        if tool_count and not NATIVE:
            assert len(attrs) == 128
            assert child.dropped_attributes > 0
        assert "synthetic-key" not in str(attrs)
    finally:
        instrumentor.uninstrument()
        provider.shutdown()
