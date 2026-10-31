"""CometAPI attribution through the real SDK and a loopback OTLP/HTTP collector."""

import asyncio
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from importlib import import_module
from threading import Thread

import openai
import pytest
from fi_instrumentation import TraceConfig
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
from opentelemetry.proto.collector.trace.v1.trace_service_pb2 import (
    ExportTraceServiceRequest,
)
from opentelemetry.proto.trace.v1.trace_pb2 import Status
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from traceai_openai import OpenAIInstrumentor
from traceai_openai._providers import provider_from_host

# OpenAI 3.x uses httpx2; the supported 1.x and 2.x SDKs use httpx.
httpx = import_module("httpx2" if int(openai.__version__.split(".")[0]) >= 3 else "httpx")
BASE_URL = "https://api.cometapi.com/v1"
REQUEST_MODEL = "requested-model"
RESPONSE_MODEL = "served-model"


@pytest.mark.parametrize(
    "host,expected",
    [
        ("api.cometapi.com", "cometapi"),
        ("API.CometAPI.com.", "cometapi"),
        ("api.cometapi.com.customer.example", "openai"),
        ("proxy.api.cometapi.com", "openai"),
        ("notapi.cometapi.com", "openai"),
        ("cometapi.com", "openai"),
        ("api.openai.com", "openai"),
        ("localhost", "openai"),
        (None, "openai"),
    ],
)
def test_cometapi_requires_the_exact_api_host(host, expected):
    assert provider_from_host(host) == expected


@pytest.fixture
def wire_spans():
    spans = []
    payloads = []

    class Collector(BaseHTTPRequestHandler):
        def do_POST(self):
            assert self.path == "/v1/traces"
            assert self.headers["Content-Type"] == "application/x-protobuf"
            payload = self.rfile.read(int(self.headers["Content-Length"]))
            payloads.append(payload)
            message = ExportTraceServiceRequest.FromString(payload)
            for resource in message.resource_spans:
                for scope in resource.scope_spans:
                    spans.extend(scope.spans)
            self.send_response(200)
            self.send_header("Content-Length", "0")
            self.end_headers()

        def log_message(self, *_args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Collector)
    thread = Thread(target=server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True)
    thread.start()
    provider = TracerProvider()
    provider.add_span_processor(
        SimpleSpanProcessor(
            OTLPSpanExporter(
                endpoint=f"http://127.0.0.1:{server.server_port}/v1/traces",
                headers={},
                timeout=2,
            )
        )
    )
    instrumentor = OpenAIInstrumentor()
    instrumentor.instrument(tracer_provider=provider, config=TraceConfig())
    try:
        yield spans
    finally:
        instrumentor.uninstrument()
        provider.shutdown()
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)
        assert b"synthetic-cometapi-key" not in b"".join(payloads)


def response_for(request, outcome):
    assert request.url.host == "api.cometapi.com"
    assert request.url.path == "/v1/chat/completions"
    assert request.headers["authorization"] == "Bearer synthetic-cometapi-key"
    body = json.loads(request.content)
    assert body["model"] == REQUEST_MODEL
    assert body["stream"] is (outcome == "stream")
    if outcome == "error":
        return httpx.Response(401, json={"error": {"message": "synthetic failure"}})
    if outcome == "stream":
        chunks = [
            {
                "id": "c",
                "object": "chat.completion.chunk",
                "created": 1,
                "model": RESPONSE_MODEL,
                "choices": [
                    {
                        "index": 0,
                        "finish_reason": "stop",
                        "delta": {"role": "assistant", "content": "hello"},
                    }
                ],
            },
            {
                "id": "c",
                "object": "chat.completion.chunk",
                "created": 1,
                "model": RESPONSE_MODEL,
                "choices": [],
                "usage": {"prompt_tokens": 5, "completion_tokens": 2, "total_tokens": 7},
            },
        ]
        body = "".join(f"data: {json.dumps(chunk)}\n\n" for chunk in chunks)
        return httpx.Response(
            200,
            headers={"content-type": "text/event-stream"},
            content=(body + "data: [DONE]\n\n").encode(),
        )
    return httpx.Response(
        200,
        json={
            "id": "c",
            "object": "chat.completion",
            "created": 1,
            "model": RESPONSE_MODEL,
            "choices": [
                {
                    "index": 0,
                    "finish_reason": "stop",
                    "message": {"role": "assistant", "content": "hello"},
                }
            ],
            "usage": {"prompt_tokens": 5, "completion_tokens": 2, "total_tokens": 7},
        },
    )


@pytest.mark.parametrize("use_async", [False, True], ids=["sync", "async"])
@pytest.mark.parametrize("outcome", ["success", "stream", "error"])
def test_cometapi_chat_attribution_on_otlp_wire(wire_spans, use_async, outcome):
    kwargs = {
        "model": REQUEST_MODEL,
        "messages": [{"role": "user", "content": "hi"}],
        "stream": outcome == "stream",
    }
    if outcome == "stream":
        kwargs["stream_options"] = {"include_usage": True}
    client_kwargs = {
        "base_url": BASE_URL,
        "api_key": "synthetic-cometapi-key",
        "max_retries": 0,
    }
    transport = httpx.MockTransport(lambda request: response_for(request, outcome))

    async def run_async():
        async with openai.AsyncOpenAI(
            **client_kwargs,
            http_client=httpx.AsyncClient(transport=transport, trust_env=False),
        ) as client:
            result = await client.chat.completions.create(**kwargs)
            if outcome == "stream":
                assert wire_spans == []  # Streaming spans end after consumption.
                text = ""
                async for chunk in result:
                    if chunk.choices:
                        text += chunk.choices[0].delta.content or ""
                assert text == "hello"
            else:
                assert result.choices[0].message.content == "hello"

    def run_sync():
        with openai.OpenAI(
            **client_kwargs,
            http_client=httpx.Client(transport=transport, trust_env=False),
        ) as client:
            result = client.chat.completions.create(**kwargs)
            if outcome == "stream":
                assert wire_spans == []
                assert "".join(
                    chunk.choices[0].delta.content or ""
                    for chunk in result if chunk.choices
                ) == "hello"
            else:
                assert result.choices[0].message.content == "hello"

    def run():
        if use_async:
            asyncio.run(run_async())
        else:
            run_sync()

    if outcome == "error":
        with pytest.raises(openai.AuthenticationError):
            run()
    else:
        run()

    assert len(wire_spans) == 1  # No duplicate provider or SDK spans.
    span = wire_spans[0]
    attrs = {attribute.key: attribute.value for attribute in span.attributes}
    assert attrs["gen_ai.provider.name"].string_value == "cometapi"
    assert attrs["gen_ai.request.model"].string_value == (
        RESPONSE_MODEL if outcome == "success" else REQUEST_MODEL
    )
    assert span.status.code == (Status.STATUS_CODE_ERROR if outcome == "error" else Status.STATUS_CODE_OK)
    if outcome != "error":
        assert attrs["output.value"].string_value == "hello"
        assert attrs["gen_ai.usage.input_tokens"].int_value == 5
        assert attrs["gen_ai.usage.output_tokens"].int_value == 2
