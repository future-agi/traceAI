"""Vercel client attribution through the real SDK and a localhost OTLP receiver.

No gateway or model API is called. Match the SDK's HTTP transport (httpx on
OpenAI 1.x, httpx2 on 3.x) rather than substituting a different HTTP client.
"""

import asyncio
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from importlib.metadata import version
from threading import Thread

import openai
import pytest
from fi_instrumentation import TraceConfig, register
from fi_instrumentation.fi_types import ProjectType
from opentelemetry.proto.collector.trace.v1.trace_service_pb2 import (
    ExportTraceServiceRequest,
)
from packaging.version import Version
from traceai_openai import OpenAIInstrumentor
from traceai_openai._providers import provider_from_host

try:
    from openai._base_client import httpx2 as sdk_httpx
except ImportError:
    from openai._base_client import httpx as sdk_httpx

REQUEST_MODEL = "openai/requested-model"
RESPONSE_MODEL = "other-provider/fallback-model"
PROMPT = "synthetic-private-prompt"
ANSWER = "synthetic-private-answer"
ERROR = "synthetic-private-gateway-error"
TOOL_DESCRIPTION = "synthetic-private-tool-description"
GATEWAY_KEY = "synthetic-gateway-key"


@pytest.mark.parametrize(
    "host,expected",
    [
        ("ai-gateway.vercel.sh", "vercel"),
        ("AI-GATEWAY.VERCEL.SH.", "vercel"),
        ("ai-gateway.vercel.sh.customer.example", "openai"),
        ("proxy.ai-gateway.vercel.sh", "openai"),
        ("not-ai-gateway.vercel.sh", "openai"),
        ("vercel.sh", "openai"),
        ("localhost", "openai"),
        ("127.0.0.1", "openai"),
        ("api.openai.com", "openai"),
        (None, "openai"),
    ],
)
def test_only_the_documented_gateway_host_is_attributed(host, expected):
    assert provider_from_host(host) == expected


@pytest.fixture
def collector(monkeypatch):
    requests = []

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            body = self.rfile.read(int(self.headers["Content-Length"]))
            requests.append((self.path, self.headers, body))
            self.send_response(200)
            self.send_header("Content-Type", "application/x-protobuf")
            self.send_header("Content-Length", "0")
            self.end_headers()

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    monkeypatch.setenv("FI_BASE_URL", f"http://127.0.0.1:{server.server_port}")
    monkeypatch.setenv("FI_API_KEY", "synthetic-fi-key")
    monkeypatch.setenv("FI_SECRET_KEY", "synthetic-fi-secret")
    monkeypatch.setenv("NO_PROXY", "127.0.0.1,localhost")
    monkeypatch.delenv("OTEL_EXPORTER_OTLP_TRACES_COMPRESSION", raising=False)
    monkeypatch.delenv("OTEL_EXPORTER_OTLP_COMPRESSION", raising=False)
    try:
        yield requests
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def gateway_response(outcome, with_usage):
    if outcome == "error":
        return sdk_httpx.Response(
            429, json={"error": {"message": ERROR, "type": "rate_limit_error"}}
        )
    usage = {"prompt_tokens": 5, "completion_tokens": 2, "total_tokens": 7}
    common = {"id": "chatcmpl-synthetic", "created": 1, "model": RESPONSE_MODEL}
    if outcome == "success":
        body = {
            **common,
            "object": "chat.completion",
            "choices": [
                {
                    "index": 0,
                    "finish_reason": "stop",
                    "message": {"role": "assistant", "content": ANSWER},
                }
            ],
        }
        if with_usage:
            body["usage"] = usage
        return sdk_httpx.Response(200, json=body)
    chunks = [
        {
            **common,
            "object": "chat.completion.chunk",
            "choices": [
                {
                    "index": 0,
                    "delta": {"role": "assistant", "content": ANSWER},
                    "finish_reason": "stop",
                }
            ],
        }
    ]
    if with_usage:
        chunks.append(
            {**common, "object": "chat.completion.chunk", "choices": [], "usage": usage}
        )
    body = "".join(f"data: {json.dumps(chunk)}\n\n" for chunk in chunks)
    return sdk_httpx.Response(
        200,
        headers={"Content-Type": "text/event-stream"},
        content=(body + "data: [DONE]\n\n").encode(),
    )


def attributes(items):
    return {
        item.key: getattr(item.value, item.value.WhichOneof("value")) for item in items
    }


@pytest.mark.parametrize("use_async", [False, True], ids=["sync", "async"])
@pytest.mark.parametrize("outcome", ["success", "stream", "error"])
@pytest.mark.parametrize("hide_content", [False, True], ids=["content", "private"])
@pytest.mark.parametrize("with_usage", [False, True], ids=["no-usage", "usage"])
def test_gateway_client_otlp(
    collector, monkeypatch, use_async, outcome, hide_content, with_usage
):
    # The package lock still uses core 1.1.0. Exercise attribution there too,
    # but require the strengthened metadata privacy only with the guide's core.
    private_metadata = Version(version("fi-instrumentation-otel")) >= Version("1.2.2")
    monkeypatch.setenv("FI_HIDE_ERROR_DETAILS", str(hide_content).lower())
    provider = register(
        project_name="vercel-gateway-contract",
        project_type=ProjectType.OBSERVE,
        batch=False,
        verbose=False,
    )
    instrumentor = OpenAIInstrumentor()
    instrumentor.instrument(
        tracer_provider=provider,
        config=TraceConfig(
            hide_inputs=hide_content,
            hide_outputs=hide_content,
            hide_llm_invocation_parameters=hide_content,
        ),
    )
    gateway_requests = []

    def handle(request):
        gateway_requests.append(request)
        assert request.url.host == "ai-gateway.vercel.sh"
        assert request.url.path == "/v1/chat/completions"
        assert request.headers["authorization"] == f"Bearer {GATEWAY_KEY}"
        assert "x-api-key" not in request.headers
        assert "x-secret-key" not in request.headers
        assert json.loads(request.content)["model"] == REQUEST_MODEL
        return gateway_response(outcome, with_usage)

    client_args = {
        "api_key": GATEWAY_KEY,
        "base_url": "https://ai-gateway.vercel.sh/v1",
        "max_retries": 0,
    }
    call_args = {
        "model": REQUEST_MODEL,
        "messages": [{"role": "user", "content": PROMPT}],
        "tools": [
            {
                "type": "function",
                "function": {
                    "name": "lookup",
                    "description": TOOL_DESCRIPTION,
                    "parameters": {"type": "object", "properties": {}},
                },
            }
        ],
        "stream": outcome == "stream",
    }
    if outcome == "stream" and with_usage:
        call_args["stream_options"] = {"include_usage": True}

    def run_sync():
        with openai.OpenAI(
            **client_args,
            http_client=sdk_httpx.Client(
                transport=sdk_httpx.MockTransport(handle), trust_env=False
            ),
        ) as client:
            response = client.chat.completions.create(**call_args)
            if outcome == "stream":
                assert not collector, (
                    "Do not finish the span before consuming the stream"
                )
                list(response)
            else:
                assert response.choices[0].message.content == ANSWER

    async def run_async():
        async with openai.AsyncOpenAI(
            **client_args,
            http_client=sdk_httpx.AsyncClient(
                transport=sdk_httpx.MockTransport(handle), trust_env=False
            ),
        ) as client:
            response = await client.chat.completions.create(**call_args)
            if outcome == "stream":
                assert not collector, (
                    "Do not finish the span before consuming the stream"
                )
                async for _ in response:
                    pass
            else:
                assert response.choices[0].message.content == ANSWER

    def run():
        if use_async:
            asyncio.run(run_async())
        else:
            run_sync()

    try:
        if outcome == "error":
            with pytest.raises(openai.RateLimitError, match=ERROR):
                run()
        else:
            run()
        assert provider.force_flush()
    finally:
        instrumentor.uninstrument()
        provider.shutdown()

    assert len(gateway_requests) == 1
    assert len(collector) == 1
    path, headers, body = collector[0]
    assert path == "/tracer/v1/traces"
    assert headers["X-Api-Key"] == "synthetic-fi-key"
    assert headers["X-Secret-Key"] == "synthetic-fi-secret"
    assert "Authorization" not in headers
    assert GATEWAY_KEY.encode() not in body
    wire = ExportTraceServiceRequest.FromString(body)
    assert len(wire.resource_spans) == 1
    resource_span = wire.resource_spans[0]
    resource = attributes(resource_span.resource.attributes)
    assert resource["project_name"] == "vercel-gateway-contract"
    assert resource["project_type"] == ProjectType.OBSERVE.value
    spans = [span for scope in resource_span.scope_spans for span in scope.spans]
    assert len(spans) == 1, "No synthetic gateway/provider-attempt spans"
    span = spans[0]
    attrs = attributes(span.attributes)
    assert attrs["gen_ai.provider.name"] == "vercel"
    assert attrs["gen_ai.request.model"] == (
        RESPONSE_MODEL if outcome == "success" else REQUEST_MODEL
    )
    assert span.status.code == (2 if outcome == "error" else 1)
    assert not any(key.startswith("vercel.ai_gateway.") for key in attrs)
    if with_usage and outcome != "error":
        assert attrs["gen_ai.usage.input_tokens"] == 5
        assert attrs["gen_ai.usage.output_tokens"] == 2
        assert attrs["gen_ai.usage.total_tokens"] == 7
    else:
        assert not any(key.startswith("gen_ai.usage.") for key in attrs)
    if hide_content:
        for value in (PROMPT, ANSWER):
            assert value.encode() not in body
        if private_metadata:
            for value in (ERROR, TOOL_DESCRIPTION):
                assert value.encode() not in body
        assert attrs["input.value"] == "__REDACTED__"
        assert "gen_ai.request.parameters" not in attrs
    else:
        assert PROMPT.encode() in body
        assert TOOL_DESCRIPTION.encode() in body
        assert (ERROR if outcome == "error" else ANSWER).encode() in body
