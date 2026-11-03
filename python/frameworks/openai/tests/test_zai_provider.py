"""Z.ai attribution through the shared OpenAI client, including OTLP wire output.

API requests use MockTransport; only the local OTLP receiver uses a socket.
Official endpoints: https://docs.z.ai/guides/develop/openai/python and
https://docs.z.ai/devpack/quick-start (Chat Completions routes only).
"""

import asyncio
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
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
from traceai_openai._providers import provider_from_host

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

BASE_URLS = [
    "https://api.z.ai/api/paas/v4/",
    "https://api.z.ai/api/coding/paas/v4/",
]
PROMPT = "synthetic-zai-private-prompt"


@pytest.mark.parametrize("host,expected", [
    ("api.z.ai", "zai"),
    ("API.Z.AI.", "zai"),
    ("api.z.ai.customer.example", "openai"),
    ("proxy.api.z.ai", "openai"),
    ("notapi.z.ai", "openai"),
    ("z.ai", "openai"),
    ("api.zai.example", "openai"),
    ("localhost", "openai"),
    ("api.openai.com", "openai"),
    ("api.x.ai", "xai"),
    (None, "openai"),
])
def test_zai_exact_host(host, expected):
    assert provider_from_host(host) == expected


@pytest.fixture
def otlp_tracer():
    payloads = []

    class Receiver(BaseHTTPRequestHandler):
        def do_POST(self):
            payloads.append((
                self.path,
                self.headers.get("Content-Type"),
                self.rfile.read(int(self.headers["Content-Length"])),
            ))
            self.send_response(200)
            self.send_header("Content-Length", "0")
            self.end_headers()

        def log_message(self, *_args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Receiver)
    thread = Thread(target=server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True)
    thread.start()
    provider = TracerProvider()
    session = requests.Session()
    session.trust_env = False
    exporter = OTLPSpanExporter(
        endpoint=f"http://127.0.0.1:{server.server_port}/v1/traces",
        session=session,
        timeout=3,
    )
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    instrumentor = OpenAIInstrumentor()

    def instrument(hide_content):
        instrumentor.instrument(
            tracer_provider=provider,
            config=TraceConfig(
                hide_inputs=hide_content,
                hide_outputs=hide_content,
                hide_llm_invocation_parameters=hide_content,
            ),
        )
        return payloads

    try:
        yield instrument
    finally:
        instrumentor.uninstrument()
        provider.shutdown()
        session.close()
        server.shutdown()
        server.server_close()
        thread.join(timeout=3)


def only_wire_span(payloads):
    assert len(payloads) == 1
    path, content_type, body = payloads[0]
    assert path == "/v1/traces"
    assert content_type == "application/x-protobuf"
    request = ExportTraceServiceRequest.FromString(body)
    spans = [span for resource in request.resource_spans
             for scope in resource.scope_spans for span in scope.spans]
    assert len(spans) == 1
    return spans[0]


@pytest.mark.parametrize("base_url", BASE_URLS, ids=["general", "coding-plan"])
@pytest.mark.parametrize("use_async", [False, True], ids=["sync", "async"])
@pytest.mark.parametrize("outcome", ["success", "stream", "error"])
@pytest.mark.parametrize("hide_content", [False, True], ids=["content", "masked"])
def test_zai_chat_provider_on_otlp_wire(otlp_tracer, base_url, use_async, outcome, hide_content):
    payloads = otlp_tracer(hide_content)
    seen = []

    def handler(request):
        seen.append(request)
        assert request.url.path == base_url.split("api.z.ai")[1] + "chat/completions"
        assert json.loads(request.content)["messages"][0]["content"] == PROMPT
        if outcome == "error":
            return unauthorized(request)
        if outcome == "stream":
            return chat_stream()
        return httpx.Response(200, json=chat_completion())

    kwargs = {
        "model": REQUEST_MODEL,
        "messages": [{"role": "user", "content": PROMPT}],
        "stream": outcome == "stream",
    }

    async def run_async():
        async with async_client(handler, base_url=base_url) as c:
            response = await c.chat.completions.create(**kwargs)
            if outcome == "stream":
                assert "".join([chunk.choices[0].delta.content or ""
                                async for chunk in response]) == ANSWER
            else:
                assert response.choices[0].message.content == ANSWER

    def run_sync():
        with client(handler, base_url=base_url) as c:
            response = c.chat.completions.create(**kwargs)
            if outcome == "stream":
                assert "".join(chunk.choices[0].delta.content or ""
                               for chunk in response) == ANSWER
            else:
                assert response.choices[0].message.content == ANSWER

    def run():
        asyncio.run(run_async()) if use_async else run_sync()

    if outcome == "error":
        with pytest.raises(openai.AuthenticationError):
            run()
    else:
        run()

    assert len(seen) == 1
    span = only_wire_span(payloads)
    attrs = {item.key: item.value for item in span.attributes}
    assert attrs["gen_ai.provider.name"].string_value == "zai"
    assert attrs["gen_ai.request.model"].string_value == (
        RESPONSE_MODEL if outcome == "success" else REQUEST_MODEL
    )
    assert span.status.code == (Status.STATUS_CODE_ERROR if outcome == "error" else Status.STATUS_CODE_OK)
    if outcome == "success":
        assert attrs["gen_ai.usage.input_tokens"].int_value == 5
        assert attrs["gen_ai.usage.output_tokens"].int_value == 2
    if hide_content:
        assert PROMPT.encode() not in payloads[0][2]
        assert ANSWER.encode() not in payloads[0][2]
    else:
        assert PROMPT.encode() in payloads[0][2]
    assert b"test-vendor-key" not in payloads[0][2]


@pytest.mark.parametrize("base_url,expected", [
    ("https://API.Z.AI./api/paas/v4/", "zai"),
    ("https://api.z.ai.customer.example/api/paas/v4/", "openai"),
    ("https://proxy.api.z.ai/api/paas/v4/", "openai"),
    ("https://notapi.z.ai/api/paas/v4/", "openai"),
    ("https://api.z.ai@customer.example/api/paas/v4/", "openai"),
    ("https://customer.example/api.z.ai/", "openai"),
    ("https://api.x.ai/v1/", "xai"),
])
def test_zai_url_controls_on_otlp_wire(otlp_tracer, base_url, expected):
    payloads = otlp_tracer(False)
    with client(lambda _: httpx.Response(200, json=chat_completion()), base_url=base_url) as c:
        c.chat.completions.create(model=REQUEST_MODEL, messages=[{"role": "user", "content": PROMPT}])
    attrs = {item.key: item.value for item in only_wire_span(payloads).attributes}
    assert attrs["gen_ai.provider.name"].string_value == expected
