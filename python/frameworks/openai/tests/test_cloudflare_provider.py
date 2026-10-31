"""Cloudflare attribution through the real SDK and a localhost OTLP/HTTP collector."""

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
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from traceai_openai import OpenAIInstrumentor
from traceai_openai._providers import provider_from_cloudflare

# OpenAI 3.x uses httpx2; earlier supported SDKs use httpx.
httpx = import_module(
    "httpx2" if int(openai.__version__.split(".")[0]) >= 3 else "httpx"
)
COMPAT = "https://gateway.ai.cloudflare.com/v1/private-account/private-gateway/compat"
REST = "https://api.cloudflare.com/client/v4/accounts/private-account/ai/v1"


@pytest.fixture
def wire_spans():
    spans = []
    payloads = []

    class Collector(BaseHTTPRequestHandler):
        def do_POST(self):
            payload = self.rfile.read(int(self.headers["Content-Length"]))
            payloads.append(payload)
            message = ExportTraceServiceRequest.FromString(payload)
            for resource in message.resource_spans:
                for scope in resource.scope_spans:
                    spans.extend(scope.spans)
            self.send_response(200)
            self.end_headers()

        def log_message(self, *_args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Collector)
    thread = Thread(
        target=server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True
    )
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
        # Only the endpoint host/port may be recorded, never URL credentials or IDs.
        wire = b"".join(payloads)
        for private in (
            b"private-account",
            b"private-gateway",
            b"test-api-key",
            b"private-user",
            b"private-password",
            b"private-query",
        ):
            assert private not in wire


def response_for(request, outcome):
    model = json.loads(request.content)["model"]
    if outcome == "error":
        return httpx.Response(401, json={"error": {"message": "synthetic failure"}})
    if outcome == "stream":
        chunk = {
            "id": "c",
            "object": "chat.completion.chunk",
            "created": 1,
            "model": model,
            "choices": [
                {
                    "index": 0,
                    "finish_reason": "stop",
                    "delta": {"role": "assistant", "content": "hello"},
                }
            ],
        }
        return httpx.Response(
            200,
            headers={"content-type": "text/event-stream"},
            content=f"data: {json.dumps(chunk)}\n\ndata: [DONE]\n\n".encode(),
        )
    if request.url.path.endswith("/responses"):
        return httpx.Response(
            200,
            json={
                "id": "resp_1",
                "object": "response",
                "created_at": 1,
                "model": model,
                "status": "completed",
                "parallel_tool_calls": False,
                "tool_choice": "auto",
                "tools": [],
                "output": [],
            },
        )
    return httpx.Response(
        200,
        json={
            "id": "c",
            "object": "chat.completion",
            "created": 1,
            "model": model,
            "choices": [
                {
                    "index": 0,
                    "finish_reason": "stop",
                    "message": {"role": "assistant", "content": "hello"},
                }
            ],
        },
    )


def call(
    base_url, model, outcome="success", use_async=False, api="chat", extra_body=None
):
    def handler(request):
        return response_for(request, outcome)

    kwargs = {"model": model}
    if extra_body is not None:
        kwargs["extra_body"] = extra_body
    if api == "chat":
        kwargs.update(
            messages=[{"role": "user", "content": "hi"}], stream=outcome == "stream"
        )
    else:
        kwargs["input"] = "hi"

    async def run_async():
        async with openai.AsyncOpenAI(
            base_url=base_url,
            api_key="test-api-key",
            max_retries=0,
            http_client=httpx.AsyncClient(
                transport=httpx.MockTransport(handler), trust_env=False
            ),
        ) as client:
            create = (
                client.chat.completions.create
                if api == "chat"
                else client.responses.create
            )
            response = await create(**kwargs)
            if outcome == "stream":
                async for _ in response:
                    pass

    def run_sync():
        with openai.OpenAI(
            base_url=base_url,
            api_key="test-api-key",
            max_retries=0,
            http_client=httpx.Client(
                transport=httpx.MockTransport(handler), trust_env=False
            ),
        ) as client:
            create = (
                client.chat.completions.create
                if api == "chat"
                else client.responses.create
            )
            response = create(**kwargs)
            if outcome == "stream":
                list(response)

    if outcome == "error":
        with pytest.raises(openai.AuthenticationError):
            asyncio.run(run_async()) if use_async else run_sync()
    else:
        asyncio.run(run_async()) if use_async else run_sync()


def only_attributes(spans):
    assert len(spans) == 1, "One application call must export exactly one span"
    return {
        a.key: getattr(a.value, a.value.WhichOneof("value"))
        for a in spans[0].attributes
    }


@pytest.mark.parametrize(
    "base_url,model",
    [
        (COMPAT, "google-ai-studio/gemini-2.5-flash"),
        (REST, "google/gemini-3-flash"),
    ],
)
@pytest.mark.parametrize("use_async", [False, True])
@pytest.mark.parametrize("outcome", ["success", "stream", "error"])
def test_google_route_on_otlp_wire(wire_spans, base_url, model, use_async, outcome):
    call(base_url, model, outcome, use_async)
    attrs = only_attributes(wire_spans)
    assert attrs["gen_ai.provider.name"] == "google"
    assert attrs["gen_ai.request.model"] == model
    assert attrs["server.address"] == httpx.URL(base_url).host
    assert wire_spans[0].status.code == (2 if outcome == "error" else 1)


@pytest.mark.parametrize(
    "base_url,model,provider",
    [
        (COMPAT, "openai/gpt-4.1", "openai"),
        (COMPAT, "anthropic/claude-sonnet-4", "anthropic"),
        (REST, "openai/gpt-4.1", "openai"),
        (REST, "anthropic/claude-sonnet-4", "anthropic"),
        (REST, "xai/grok-3", "xai"),
        (REST, "@cf/moonshotai/kimi-k2.6", "cloudflare"),
    ],
)
def test_documented_model_routes(wire_spans, base_url, model, provider):
    call(base_url, model)
    assert only_attributes(wire_spans)["gen_ai.provider.name"] == provider


@pytest.mark.parametrize("base_url", [COMPAT, REST])
@pytest.mark.parametrize(
    "model", ["unknown/gpt-4.1", "gemini-3-flash", "google/", "google.evil/gemini"]
)
def test_unknown_model_does_not_guess(wire_spans, base_url, model):
    call(base_url, model)
    attrs = only_attributes(wire_spans)
    assert "gen_ai.provider.name" not in attrs
    assert attrs["server.address"] == httpx.URL(base_url).host


@pytest.mark.parametrize("base_url", [COMPAT, REST])
@pytest.mark.parametrize("use_async", [False, True])
@pytest.mark.parametrize("outcome", ["success", "stream", "error"])
def test_dynamic_route_stays_ambiguous(wire_spans, base_url, use_async, outcome):
    call(base_url, "dynamic/google", outcome, use_async)
    attrs = only_attributes(wire_spans)
    assert "gen_ai.provider.name" not in attrs
    assert attrs["server.address"] == httpx.URL(base_url).host


@pytest.mark.parametrize(
    "base_url",
    [
        COMPAT.replace("/compat", "/unsupported"),
        REST.replace("/ai/v1", "/ai/v2"),
        REST.replace("/private-account", ""),
    ],
)
def test_prefix_outside_documented_route_does_not_guess(wire_spans, base_url):
    call(base_url, "google/gemini-3-flash")
    assert "gen_ai.provider.name" not in only_attributes(wire_spans)


@pytest.mark.parametrize("model", [None, "", 42, {}, "google", "google/"])
def test_missing_or_invalid_model_has_no_provider(model):
    assert (
        provider_from_cloudflare(
            host="api.cloudflare.com",
            path="/client/v4/accounts/a/ai/v1/",
            model=model,
        )
        is None
    )


@pytest.mark.parametrize(
    "base_url,provider",
    [
        ("https://api.openai.com/v1", "openai"),
        ("https://generativelanguage.googleapis.com/v1beta/openai", "google"),
        ("https://gateway.ai.cloudflare.com.evil.example/v1/a/g/compat", "openai"),
        (
            "https://api.cloudflare.com.evil.example/client/v4/accounts/a/ai/v1",
            "openai",
        ),
        ("http://localhost:9999/v1", "openai"),
    ],
)
def test_non_cloudflare_attribution_is_unchanged(wire_spans, base_url, provider):
    call(base_url, "anthropic/claude-sonnet-4")
    assert only_attributes(wire_spans)["gen_ai.provider.name"] == provider


@pytest.mark.parametrize("use_async", [False, True])
def test_rest_responses_api(wire_spans, use_async):
    call(REST, "google/gemini-3-flash", use_async=use_async, api="responses")
    assert only_attributes(wire_spans)["gen_ai.provider.name"] == "google"


@pytest.mark.parametrize("use_async", [False, True])
@pytest.mark.parametrize(
    "model,override,provider",
    [
        ("google/gemini-3-flash", "dynamic/google", None),
        ("dynamic/google", "google/gemini-3-flash", "google"),
    ],
)
def test_body_override_controls_attribution(
    wire_spans, use_async, model, override, provider
):
    call(REST, model, "stream", use_async, extra_body={"model": override})
    attrs = only_attributes(wire_spans)
    assert attrs.get("gen_ai.provider.name") == provider
    assert attrs["gen_ai.request.model"] == override


def test_endpoint_identity_excludes_private_url_parts(wire_spans):
    url = (
        "https://private-user:private-password@API.Cloudflare.COM.:8443/"
        "client/v4/accounts/private-account/ai/v1/?token=private-query"
    )
    call(url, "google/gemini-3-flash")
    attrs = only_attributes(wire_spans)
    assert attrs["gen_ai.provider.name"] == "google"
    assert attrs["server.address"] == "api.cloudflare.com"
    assert attrs["server.port"] == 8443
