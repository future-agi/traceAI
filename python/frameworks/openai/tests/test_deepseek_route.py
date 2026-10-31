"""Real SDK, synthetic provider transport, and localhost OTLP migration contract."""

import asyncio
import json
import threading
from dataclasses import fields
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import openai
import pytest
from fi_instrumentation import TraceConfig
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
from opentelemetry.proto.collector.trace.v1.trace_service_pb2 import (
    ExportTraceServiceRequest,
)
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor

DeepSeekInstrumentor = pytest.importorskip("traceai_deepseek").DeepSeekInstrumentor

try:
    from openai._base_client import httpx2 as httpx
except ImportError:
    from openai._base_client import httpx

MODERN = tuple(map(int, openai.__version__.split(".")[:2])) >= (1, 69)
if MODERN:
    from traceai_openai import OpenAIInstrumentor

BASE = dict(id="completion-offline", created=1, model="deepseek-reasoner")
REASONING = "PRIVATE_REASONING_7382"
ANSWER = "PRIVATE_ANSWER_1294"
TOOL_ARGS = '{"secret":"PRIVATE_TOOL_9271"}'
USAGE = dict(
    prompt_tokens=5,
    completion_tokens=7,
    total_tokens=12,
    prompt_cache_hit_tokens=3,
    prompt_cache_miss_tokens=2,
    completion_tokens_details=dict(reasoning_tokens=4),
)
TOOL = dict(
    id="call-offline",
    type="function",
    function=dict(name="lookup", arguments=TOOL_ARGS),
)
MESSAGE = dict(
    role="assistant", content=ANSWER, reasoning_content=REASONING, tool_calls=[TOOL]
)
BODY = dict(
    BASE,
    object="chat.completion",
    usage=USAGE,
    choices=[dict(index=0, message=MESSAGE, finish_reason="tool_calls")],
)
RESPONSE = dict(
    id="resp-offline",
    object="response",
    created_at=1,
    status="completed",
    model="deepseek-reasoner",
    output=[
        dict(
            id="msg-offline",
            type="message",
            status="completed",
            role="assistant",
            content=[dict(type="output_text", text=ANSWER, annotations=[])],
        )
    ],
    usage=dict(
        input_tokens=5,
        output_tokens=7,
        total_tokens=12,
        input_tokens_details=dict(cached_tokens=3),
        output_tokens_details=dict(reasoning_tokens=4),
    ),
)


def respond(request):
    payload = json.loads(request.content)
    if request.url.path.endswith("/responses"):
        if payload.get("stream"):
            event = dict(
                type="response.completed", sequence_number=1, response=RESPONSE
            )
            return httpx.Response(
                200,
                headers={"content-type": "text/event-stream"},
                content="data: " + json.dumps(event) + "\n\n",
            )
        return httpx.Response(200, json=RESPONSE)
    if payload.get("stream"):
        deltas = [
            dict(role="assistant", reasoning_content=REASONING[:10]),
            dict(
                reasoning_content=REASONING[10:],
                content=ANSWER,
                tool_calls=[
                    dict(
                        TOOL,
                        index=0,
                        function=dict(name="lookup", arguments=TOOL_ARGS[:12]),
                    )
                ],
            ),
            dict(tool_calls=[dict(index=0, function=dict(arguments=TOOL_ARGS[12:]))]),
        ]
        chunks = [
            dict(
                BASE,
                object="chat.completion.chunk",
                usage=None,
                choices=[dict(index=0, delta=d, finish_reason=None)],
            )
            for d in deltas
        ]
        chunks.append(
            dict(
                BASE,
                object="chat.completion.chunk",
                usage=USAGE,
                choices=[dict(index=0, delta={}, finish_reason="tool_calls")],
            )
        )
        data = "".join("data: " + json.dumps(c) + "\n\n" for c in chunks)
        return httpx.Response(
            200,
            headers={"content-type": "text/event-stream"},
            content=data + "data: [DONE]\n\n",
        )
    return httpx.Response(200, json=BODY)


@pytest.fixture
def wire(monkeypatch):
    for field in fields(TraceConfig):
        monkeypatch.delenv(field.metadata["env_var"], raising=False)
    captured = []

    class Receiver(BaseHTTPRequestHandler):
        def do_POST(self):
            payload = self.rfile.read(int(self.headers["Content-Length"]))
            decoded = ExportTraceServiceRequest.FromString(payload)
            captured.extend(
                span
                for resource in decoded.resource_spans
                for scope in resource.scope_spans
                for span in scope.spans
            )
            self.send_response(200)
            self.end_headers()

        def log_message(self, *_args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Receiver)
    thread = threading.Thread(
        target=lambda: server.serve_forever(poll_interval=0.01), daemon=True
    )
    thread.start()
    provider = TracerProvider()
    provider.add_span_processor(
        SimpleSpanProcessor(
            OTLPSpanExporter(
                endpoint=f"http://127.0.0.1:{server.server_port}/v1/traces", timeout=5
            )
        )
    )
    instruments = []

    def start(route, **privacy):
        if route != "legacy" and not MODERN:
            pytest.skip("Shared OpenAI instrumentor requires SDK >=1.69")
        classes = dict(legacy=[DeepSeekInstrumentor])
        if MODERN:
            classes.update(
                shared=[OpenAIInstrumentor],
                legacy_first=[DeepSeekInstrumentor, OpenAIInstrumentor],
                shared_first=[OpenAIInstrumentor, DeepSeekInstrumentor],
            )
        for cls in classes[route]:
            inst = cls()
            inst.instrument(tracer_provider=provider, config=TraceConfig(**privacy))
            instruments.append(inst)
        return captured

    yield start
    for inst in reversed(instruments):
        if inst.is_instrumented_by_opentelemetry:
            inst.uninstrument()
    provider.shutdown()
    server.shutdown()
    thread.join()
    server.server_close()


def attrs(spans):
    assert len(spans) == 1
    return {
        a.key: getattr(a.value, a.value.WhichOneof("value"))
        for a in spans[0].attributes
    }


def call(
    asynchronous=False,
    streaming=False,
    mode="full",
    host="api.deepseek.com",
    responses=False,
):
    params = dict(model="deepseek-reasoner", stream=streaming)
    params.update(
        dict(input="PRIVATE_INPUT_7134")
        if responses
        else dict(messages=[dict(role="user", content="PRIVATE_INPUT_7134")])
    )

    async def run():
        async with openai.AsyncOpenAI(
            api_key="offline-placeholder",
            base_url=f"https://{host}/v1",
            http_client=httpx.AsyncClient(transport=httpx.MockTransport(respond)),
        ) as client:
            resource = client.responses if responses else client.chat.completions
            response = await resource.create(**params)
            if streaming:
                if mode == "close":
                    await response.close()
                else:
                    async with response as entered:
                        assert entered is response
                        if mode != "empty":
                            async for _chunk in response:
                                if mode == "early":
                                    break
                assert response.response.is_closed
            return response

    if asynchronous:
        return asyncio.run(run())
    with openai.OpenAI(
        api_key="offline-placeholder",
        base_url=f"https://{host}/v1",
        http_client=httpx.Client(transport=httpx.MockTransport(respond)),
    ) as client:
        resource = client.responses if responses else client.chat.completions
        response = resource.create(**params)
        if streaming:
            if mode == "close":
                response.close()
            else:
                with response as entered:
                    assert entered is response
                    if mode != "empty":
                        for _chunk in response:
                            if mode == "early":
                                break
            assert response.response.is_closed
        return response


@pytest.mark.parametrize("route", ["legacy", "shared", "legacy_first", "shared_first"])
@pytest.mark.parametrize("asynchronous", [False, True])
@pytest.mark.parametrize("streaming", [False, True])
def test_chat_migration_parity(wire, route, asynchronous, streaming):
    spans = wire(route)
    response = call(asynchronous, streaming)
    a = attrs(spans)
    assert a["gen_ai.provider.name"] == "deepseek"
    assert a["gen_ai.request.model"] == "deepseek-reasoner"
    assert a["deepseek.prompt_cache_hit_tokens"] == 3
    assert a["deepseek.prompt_cache_miss_tokens"] == 2
    assert a["deepseek.reasoning_tokens"] == 4
    assert a["gen_ai.usage.output_tokens"] == 7
    assert a["deepseek.reasoning_content"] == REASONING
    assert a["deepseek.output_reasoning"] == REASONING
    assert a["gen_ai.output.messages.0.reasoning_content"] == REASONING
    assert a["deepseek.finish_reason"] == "tool_calls"
    assert a["deepseek.tool_calls_count"] == 1
    assert a["deepseek.tool_calls.0.function.arguments"] == TOOL_ARGS
    if not streaming:
        assert response.choices[0].message.reasoning_content == REASONING


@pytest.mark.parametrize("route", ["legacy", "shared", "legacy_first", "shared_first"])
@pytest.mark.parametrize("asynchronous", [False, True])
@pytest.mark.parametrize("mode", ["early", "empty", "close"])
def test_stream_lifecycle(wire, route, asynchronous, mode):
    spans = wire(route)
    call(asynchronous, streaming=True, mode=mode)
    assert attrs(spans)["gen_ai.provider.name"] == "deepseek"


@pytest.mark.parametrize("route", ["shared", "legacy_first", "shared_first"])
@pytest.mark.parametrize("asynchronous", [False, True])
@pytest.mark.parametrize("streaming", [False, True])
def test_responses(wire, route, asynchronous, streaming):
    spans = wire(route)
    call(asynchronous, streaming, responses=True)
    assert attrs(spans)["gen_ai.provider.name"] == "deepseek"
    assert attrs(spans)["gen_ai.usage.output_tokens"] == 7


@pytest.mark.parametrize("route", ["legacy", "shared", "legacy_first", "shared_first"])
@pytest.mark.parametrize("asynchronous", [False, True])
@pytest.mark.parametrize("streaming", [False, True])
@pytest.mark.parametrize(
    "policy", ["hide_outputs", "hide_output_messages", "hide_output_text"]
)
def test_output_privacy_on_wire(wire, route, asynchronous, streaming, policy):
    spans = wire(route, **{policy: True})
    call(asynchronous, streaming)
    a = attrs(spans)
    assert a["deepseek.prompt_cache_hit_tokens"] == 3
    assert a["deepseek.reasoning_tokens"] == 4
    if policy != "hide_outputs":
        a.pop("output.value", None)  # Message/text policies do not hide raw output.
    for value in [REASONING, ANSWER, "PRIVATE_TOOL_9271"]:
        assert value not in json.dumps(a)


@pytest.mark.parametrize(
    "host",
    [
        "api.deepseek.com.evil.example",
        "proxy.example/api.deepseek.com",
        "notdeepseek.com",
    ],
)
def test_legacy_exact_host(wire, host):
    spans = wire("legacy")
    call(host=host)
    assert not spans


@pytest.mark.parametrize("route", ["legacy_first", "shared_first"])
@pytest.mark.parametrize("remove", ["legacy", "shared"])
def test_independent_teardown(wire, route, remove):
    spans = wire(route)
    (
        DeepSeekInstrumentor() if remove == "legacy" else OpenAIInstrumentor()
    ).uninstrument()
    call()
    assert attrs(spans)["deepseek.reasoning_content"] == REASONING


@pytest.mark.parametrize("route", ["legacy", "shared", "legacy_first", "shared_first"])
@pytest.mark.parametrize("asynchronous", [False, True])
def test_api_error_privacy(wire, monkeypatch, route, asynchronous):
    spans = wire(route, hide_error_details=True)

    def fail(request):
        return httpx.Response(
            400,
            json=dict(
                error=dict(message="PRIVATE_ERROR_1842", type="invalid_request_error")
            ),
        )

    monkeypatch.setattr(__import__(__name__, fromlist=["respond"]), "respond", fail)
    with pytest.raises(openai.BadRequestError, match="PRIVATE_ERROR_1842"):
        call(asynchronous)
    assert attrs(spans)["gen_ai.request.model"] == "deepseek-reasoner"
    assert spans[0].status.code == 2
    assert b"PRIVATE_ERROR_1842" not in spans[0].SerializeToString()


@pytest.mark.parametrize("route", ["legacy", "shared", "legacy_first", "shared_first"])
def test_idempotent_instrumentation(wire, route):
    spans = wire(route)
    DeepSeekInstrumentor().instrument()
    call()
    assert attrs(spans)["gen_ai.provider.name"] == "deepseek"
    # The fixture owns legacy teardown only when it started that instrumentor.
    if route == "shared":
        DeepSeekInstrumentor().uninstrument()


@pytest.mark.parametrize("route", ["legacy_first", "shared_first"])
def test_shared_configuration_owns_request(wire, route):
    spans = wire(route)
    DeepSeekInstrumentor().uninstrument()
    DeepSeekInstrumentor().instrument(config=TraceConfig(hide_outputs=True))
    call()
    assert attrs(spans)["deepseek.reasoning_content"] == REASONING


def test_legacy_does_not_defer_to_shared_without_extension_support(wire, monkeypatch):
    spans = wire("legacy_first")
    monkeypatch.setattr(OpenAIInstrumentor, "_supports_deepseek_extensions", False)
    call()
    assert len(spans) == 2  # Existing behavior with an older shared package.
    legacy = [span for span in spans if span.name == "deepseek.chat.completions"]
    assert attrs(legacy)["deepseek.reasoning_content"] == REASONING


def test_shared_non_deepseek_has_no_aliases(wire):
    spans = wire("shared")
    call(host="api.openai.com")
    assert attrs(spans)["gen_ai.provider.name"] == "openai"
    assert not any(key.startswith("deepseek.") for key in attrs(spans))


@pytest.mark.parametrize("asynchronous", [False, True])
def test_explicit_configuration_overrides_environment(wire, monkeypatch, asynchronous):
    spans = wire("shared", hide_outputs=False)
    monkeypatch.setenv("FI_HIDE_OUTPUTS", "true")
    call(asynchronous, streaming=True)
    assert attrs(spans)["deepseek.reasoning_content"] == REASONING
