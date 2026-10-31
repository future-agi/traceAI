"""Novita uses the shared OpenAI instrumentor, not a separate SDK wrapper.

Vendor traffic is synthetic. Traces cross a localhost OTLP/HTTP protobuf wire
before assertions, covering sync/async success, streams, errors and look-alikes.
"""

import asyncio
import json
from http.server import BaseHTTPRequestHandler, HTTPServer
from queue import Queue
from threading import Thread

import httpx
import openai
import pytest
import requests
from fi_instrumentation import TraceConfig
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
from opentelemetry.proto.collector.trace.v1.trace_service_pb2 import ExportTraceServiceRequest
from opentelemetry.proto.trace.v1.trace_pb2 import Status
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor

from traceai_openai import OpenAIInstrumentor

from .test_openai_compatible_spans import (
    ANSWER,
    REQUEST_MODEL,
    RESPONSE_MODEL,
    async_client,
    chat_completion,
    chat_stream,
    client,
    unauthorized,
)


@pytest.fixture(scope="module")
def otlp_receiver():
    received = Queue()

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            body = self.rfile.read(int(self.headers["Content-Length"]))
            received.put((self.path, self.headers["Content-Type"], body))
            self.send_response(200)
            self.send_header("Content-Type", "application/x-protobuf")
            self.send_header("Content-Length", "0")
            self.end_headers()

        def log_message(self, *_args):
            pass

    server = HTTPServer(("127.0.0.1", 0), Handler)
    worker = Thread(target=server.serve_forever, daemon=True)
    worker.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}/v1/traces", received
    finally:
        server.shutdown()
        worker.join(timeout=5)
        server.server_close()


@pytest.fixture
def wire_trace(otlp_receiver):
    endpoint, received = otlp_receiver
    assert received.empty()
    session = requests.Session()
    session.trust_env = False
    exporter = OTLPSpanExporter(endpoint=endpoint, session=session, timeout=5)
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    instrumentor = OpenAIInstrumentor()
    instrumentor.instrument(tracer_provider=provider, config=TraceConfig())
    try:
        yield received
    finally:
        instrumentor.uninstrument()
        provider.shutdown()


@pytest.mark.parametrize("base_url,expected_provider", [
    ("https://api.novita.ai/openai/v1", "novita"),
    ("https://API.NOVITA.AI./openai/v1", "novita"),
    ("https://api.novita.ai:443/openai/v1", "novita"),
    ("https://api.novita.ai.customer.example/openai/v1", "openai"),
    ("https://notapi.novita.ai/openai/v1", "openai"),
    ("https://proxy.api.novita.ai/openai/v1", "openai"),
    ("https://api.novita-ai.example/openai/v1", "openai"),
    ("https://llm-proxy.customer.example/openai/v1", "openai"),
    ("https://api.openai.com/openai/v1", "openai"),
])
@pytest.mark.parametrize("outcome", ["success", "stream", "stream-usage", "error"])
@pytest.mark.parametrize("use_async", [False, True], ids=["sync", "async"])
def test_novita_provider_over_otlp(wire_trace, base_url, expected_provider, outcome, use_async):
    calls = []
    streaming = outcome.startswith("stream")

    def handler(request):
        assert request.url.path == "/openai/v1/chat/completions"
        body = json.loads(request.content)
        assert body["model"] == REQUEST_MODEL
        assert body.get("stream", False) == streaming
        calls.append(request.url.host)
        if outcome == "error":
            return unauthorized(request)
        if streaming:
            response = chat_stream()
            if outcome == "stream-usage":
                assert body["stream_options"] == {"include_usage": True}
                usage = {
                    "id": "c", "object": "chat.completion.chunk", "created": 1,
                    "model": RESPONSE_MODEL, "choices": [],
                    "usage": {"prompt_tokens": 5, "completion_tokens": 2, "total_tokens": 7},
                }
                content = response.content.replace(
                    b"data: [DONE]", f"data: {json.dumps(usage)}\n\ndata: [DONE]".encode(),
                )
                return httpx.Response(200, headers=response.headers, content=content)
            return response
        return httpx.Response(200, json=chat_completion())

    kwargs = {
        "model": REQUEST_MODEL,
        "messages": [{"role": "user", "content": "Capital of Japan?"}],
        "max_tokens": 128,
        "stream": streaming,
    }
    if outcome == "stream-usage":
        kwargs["stream_options"] = {"include_usage": True}

    async def run_async():
        async with async_client(handler, base_url=base_url) as sdk:
            response = await sdk.chat.completions.create(**kwargs)
            if streaming:
                text = []
                async for chunk in response:
                    text.extend(choice.delta.content or "" for choice in chunk.choices)
                assert "".join(text) == ANSWER
            else:
                assert response.choices[0].message.content == ANSWER

    def run_sync():
        with client(handler, base_url=base_url) as sdk:
            response = sdk.chat.completions.create(**kwargs)
            if streaming:
                text = "".join(
                    choice.delta.content or "" for chunk in response for choice in chunk.choices
                )
                assert text == ANSWER
            else:
                assert response.choices[0].message.content == ANSWER

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

    assert calls == [httpx.URL(base_url).host]
    path, content_type, payload = wire_trace.get(timeout=5)
    assert path == "/v1/traces"
    assert content_type == "application/x-protobuf"
    message = ExportTraceServiceRequest.FromString(payload)
    spans = [span for resource in message.resource_spans
             for scope in resource.scope_spans for span in scope.spans]
    assert len(spans) == 1
    assert wire_trace.empty(), "The shared instrumentor must not duplicate spans"
    span = spans[0]
    attrs = {item.key: getattr(item.value, item.value.WhichOneof("value"))
             for item in span.attributes}
    assert attrs["gen_ai.provider.name"] == expected_provider
    assert attrs["gen_ai.request.model"] == (
        RESPONSE_MODEL if outcome == "success" else REQUEST_MODEL
    )
    assert attrs["gen_ai.span.kind"] == "LLM"
    assert span.status.code == (
        Status.STATUS_CODE_ERROR if outcome == "error" else Status.STATUS_CODE_OK
    )
    if outcome in ("success", "stream-usage"):
        assert attrs["gen_ai.usage.input_tokens"] == 5
        assert attrs["gen_ai.usage.output_tokens"] == 2
        assert attrs["gen_ai.usage.total_tokens"] == 7
    else:
        assert not any(key.startswith("gen_ai.usage.") for key in attrs)
    if streaming:
        assert attrs["output.value"] == ANSWER
    assert b"test-vendor-key" not in payload
