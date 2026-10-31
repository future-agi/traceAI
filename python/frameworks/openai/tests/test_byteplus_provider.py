"""BytePlus chat contract: real OpenAI SDK, mocked vendor, loopback OTLP/HTTP.

The fixtures model the OpenAI-compatible Chat API, not arkruntime or other
ModelArk APIs. No request reaches BytePlus and no real credentials are used.
"""

import asyncio
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import Thread

import openai
import pytest
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
from opentelemetry.proto.collector.trace.v1.trace_service_pb2 import (
    ExportTraceServiceRequest,
)
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from traceai_openai import OpenAIInstrumentor
from traceai_openai._providers import provider_from_host

# OpenAI 3.x uses httpx2; older supported versions use httpx.
if int(openai.__version__.split(".")[0]) >= 3:
    import httpx2 as httpx
else:
    import httpx


HOSTS = ("ark.ap-southeast.bytepluses.com", "ark.eu-west.bytepluses.com")
REQUEST_MODEL = "ep-synthetic-endpoint"
RESPONSE_MODEL = "synthetic-served-model"
API_KEY = "synthetic-byteplus-key-do-not-export"
ARGUMENTS = '{"city":"Tokyo"}'
USAGE = {"prompt_tokens": 9, "completion_tokens": 4, "total_tokens": 13}
TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "weather",
            "description": "Get the weather for a city",
            "parameters": {
                "type": "object",
                "properties": {"city": {"type": "string"}},
            },
        },
    }
]


@pytest.fixture
def wire_spans():
    """Decode the actual OTLP exporter request, rather than inspect SDK spans."""
    spans = []

    class Receiver(BaseHTTPRequestHandler):
        def do_POST(self):
            assert self.path == "/v1/traces"
            assert self.headers["Content-Type"] == "application/x-protobuf"
            payload = self.rfile.read(int(self.headers["Content-Length"]))
            assert API_KEY.encode() not in payload
            request = ExportTraceServiceRequest.FromString(payload)
            for resource in request.resource_spans:
                for scope in resource.scope_spans:
                    spans.extend(scope.spans)
            self.send_response(200)
            self.send_header("Content-Length", "0")
            self.end_headers()

        def log_message(self, *_args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Receiver)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    provider = TracerProvider()
    provider.add_span_processor(
        SimpleSpanProcessor(
            OTLPSpanExporter(
                endpoint=f"http://127.0.0.1:{server.server_port}/v1/traces",
                timeout=5,
            )
        )
    )
    instrumentor = OpenAIInstrumentor()
    instrumentor.instrument(tracer_provider=provider)
    try:
        yield spans
    finally:
        instrumentor.uninstrument()
        provider.shutdown()
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def response(outcome):
    if outcome == "error":
        return httpx.Response(
            401, json={"error": {"message": "Unauthorized", "type": "auth"}}
        )
    tool = "tool" in outcome
    streaming = outcome.startswith("stream")
    message = {"role": "assistant", "content": None if tool else "Tokyo"}
    if tool:
        message["tool_calls"] = [
            {
                "id": "call-weather",
                "type": "function",
                "function": {"name": "weather", "arguments": ARGUMENTS},
            }
        ]
    body = {
        "id": "chatcmpl-synthetic",
        "created": 1,
        "model": RESPONSE_MODEL,
        "object": "chat.completion",
        "usage": USAGE,
        "choices": [
            {
                "index": 0,
                "finish_reason": "tool_calls" if tool else "stop",
                "message": message,
            }
        ],
    }
    if not streaming:
        return httpx.Response(200, json=body)
    if tool:
        deltas = [
            {
                "role": "assistant",
                "tool_calls": [
                    {
                        "index": 0,
                        "id": "call-weather",
                        "type": "function",
                        "function": {"name": "weather", "arguments": '{"city":'},
                    }
                ],
            },
            {"tool_calls": [{"index": 0, "function": {"arguments": '"Tokyo"}'}}]},
        ]
    else:
        deltas = [{"role": "assistant", "content": "Tok"}, {"content": "yo"}]
    base = {
        "id": body["id"],
        "created": 1,
        "model": RESPONSE_MODEL,
        "object": "chat.completion.chunk",
    }
    chunks = [
        {**base, "choices": [{"index": 0, "delta": delta, "finish_reason": None}]}
        for delta in deltas
    ]
    chunks.append(
        {
            **base,
            "choices": [
                {
                    "index": 0,
                    "delta": {},
                    "finish_reason": "tool_calls" if tool else "stop",
                }
            ],
        }
    )
    chunks.append({**base, "choices": [], "usage": USAGE})
    content = (
        "".join(f"data: {json.dumps(chunk)}\n\n" for chunk in chunks)
        + "data: [DONE]\n\n"
    )
    return httpx.Response(
        200, headers={"content-type": "text/event-stream"}, content=content
    )


@pytest.mark.parametrize("host", HOSTS)
@pytest.mark.parametrize("use_async", [False, True], ids=["sync", "async"])
@pytest.mark.parametrize("outcome", ["chat", "stream", "tool", "stream-tool", "error"])
def test_byteplus_chat_otlp(wire_spans, host, use_async, outcome):
    streaming = outcome.startswith("stream")
    calls = []

    def handler(request):
        assert str(request.url) == f"https://{host}/api/v3/chat/completions"
        assert request.headers["authorization"] == f"Bearer {API_KEY}"
        body = json.loads(request.content)
        assert body["model"] == REQUEST_MODEL
        if streaming:
            assert body["stream_options"] == {"include_usage": True}
        if "tool" in outcome:
            assert body["tools"] == TOOLS
        calls.append(body)
        return response(outcome)

    kwargs = {
        "model": REQUEST_MODEL,
        "messages": [{"role": "user", "content": "Weather?"}],
    }
    if streaming:
        kwargs.update(stream=True, stream_options={"include_usage": True})
    if "tool" in outcome:
        kwargs["tools"] = TOOLS
    client_kwargs = {
        "base_url": f"https://{host}/api/v3",
        "api_key": API_KEY,
        "max_retries": 0,
    }

    async def run_async():
        async with openai.AsyncOpenAI(
            **client_kwargs,
            http_client=httpx.AsyncClient(
                transport=httpx.MockTransport(handler), trust_env=False
            ),
        ) as client:
            result = await client.chat.completions.create(**kwargs)
            if streaming:
                return [chunk async for chunk in result]
            return result

    def run_sync():
        with openai.OpenAI(
            **client_kwargs,
            http_client=httpx.Client(
                transport=httpx.MockTransport(handler), trust_env=False
            ),
        ) as client:
            result = client.chat.completions.create(**kwargs)
            return list(result) if streaming else result

    if outcome == "error":
        with pytest.raises(openai.AuthenticationError):
            asyncio.run(run_async()) if use_async else run_sync()
    else:
        result = asyncio.run(run_async()) if use_async else run_sync()
        if streaming:
            assert result[-1].usage.total_tokens == 13
        elif outcome == "chat":
            assert result.choices[0].message.content == "Tokyo"
        else:
            assert (
                result.choices[0].message.tool_calls[0].function.arguments == ARGUMENTS
            )

    assert len(calls) == 1
    assert len(wire_spans) == 1  # No duplicate spans or synthetic tool-execution spans.
    span = wire_spans[0]
    attrs = {
        attr.key: getattr(attr.value, attr.value.WhichOneof("value"))
        for attr in span.attributes
    }
    assert attrs["gen_ai.provider.name"] == "byteplus"
    assert attrs["gen_ai.span.kind"] == "LLM"
    assert attrs["gen_ai.request.model"] == (
        REQUEST_MODEL if streaming or outcome == "error" else RESPONSE_MODEL
    )
    assert span.status.code == (2 if outcome == "error" else 1)
    if outcome == "error":
        assert any(event.name == "exception" for event in span.events)
    else:
        assert attrs["gen_ai.usage.input_tokens"] == 9
        assert attrs["gen_ai.usage.output_tokens"] == 4
        assert attrs["gen_ai.usage.total_tokens"] == 13
        if "tool" in outcome:
            assert json.loads(attrs["gen_ai.tool.definitions"]) == TOOLS
            # The existing shared instrumentor extracts tool outputs only for
            # non-streaming chat. Streaming still records provider, definitions
            # and usage; this attribution fix does not widen that contract.
            if not streaming:
                prefix = "gen_ai.output.messages.0.message.tool_calls.0.tool_call"
                assert attrs[f"{prefix}.function.name"] == "weather"
                assert json.loads(attrs[f"{prefix}.function.arguments"]) == {
                    "city": "Tokyo"
                }
        else:
            assert attrs["output.value"] == "Tokyo"


@pytest.mark.parametrize("host", HOSTS)
def test_byteplus_host_normalization(host):
    assert provider_from_host(host.upper() + ".") == "byteplus"


@pytest.mark.parametrize(
    "host",
    [
        "ark.ap-southeast.bytepluses.com.evil.example",
        "ark.eu-west.bytepluses.com.evil.example",
        "proxy.ark.ap-southeast.bytepluses.com",
        "notark.ap-southeast.bytepluses.com",
        "ark.unknown.bytepluses.com",
        "bytepluses.com",
        "customer.example",
        "localhost",
        None,
    ],
)
def test_byteplus_mapping_does_not_guess_unknown_hosts(host):
    assert provider_from_host(host) == "openai"
