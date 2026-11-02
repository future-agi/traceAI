"""Fireworks' OpenAI-compatible route, using fake HTTP and real OTLP/HTTP.

Run with both source packages installed. No Fireworks or Future AGI endpoint is
contacted; the only socket destination is the loopback OTLP receiver.
"""

import asyncio
import json
import sys
from pathlib import Path

import httpx
import openai
import pytest
from fi_instrumentation import TraceConfig
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from packaging.version import Version

# Generic OpenAI-only jobs need not install the deprecated optional package;
# the dedicated Fireworks CI matrix installs both and must run this contract.
FireworksInstrumentor = pytest.importorskip("traceai_fireworks").FireworksInstrumentor

# Reuse the repository's OTLP receiver, rather than asserting only Python spans.
sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "tests"))
from harness import Receiver, _flatten_attributes

MODERN_SDK = Version(openai.__version__) >= Version("1.69.0")
if MODERN_SDK:
    from traceai_openai import OpenAIInstrumentor

BASE_URL = "https://api.fireworks.ai/inference/v1"
MODEL = "accounts/fireworks/models/test-model"
INPUT = "PRIVATE_INPUT_SENTINEL"
OUTPUT = "PRIVATE_OUTPUT_SENTINEL"
ARGUMENT = "PRIVATE_TOOL_ARGUMENT_SENTINEL"
ERROR = "PRIVATE_ERROR_SENTINEL"


@pytest.fixture(scope="module")
def receiver():
    with Receiver() as receiver:
        yield receiver


def response(outcome):
    usage = {"prompt_tokens": 5, "completion_tokens": 2, "total_tokens": 7}
    tool = {
        "id": "call_1",
        "type": "function",
        "function": {"name": "lookup", "arguments": json.dumps({"query": ARGUMENT})},
    }
    message = {"role": "assistant", "content": OUTPUT}
    if outcome == "tool":
        message = {"role": "assistant", "content": None, "tool_calls": [tool]}
    if outcome == "error":
        return httpx.Response(
            401, json={"error": {"message": ERROR, "type": "authentication_error"}}
        )
    if outcome in ("stream", "tool_stream"):
        deltas = (
            [{"role": "assistant", "content": OUTPUT[:10]}, {"content": OUTPUT[10:]}]
            if outcome == "stream"
            else [
                {
                    "role": "assistant",
                    "tool_calls": [
                        {
                            **tool,
                            "index": 0,
                            "function": {"name": "lookup", "arguments": '{"query": "'},
                        }
                    ],
                },
                {
                    "tool_calls": [
                        {"index": 0, "function": {"arguments": ARGUMENT + '"}'}}
                    ]
                },
            ]
        )
        chunks = [
            {
                "id": "c",
                "object": "chat.completion.chunk",
                "created": 1,
                "model": MODEL,
                "choices": [{"index": 0, "delta": delta, "finish_reason": None}],
            }
            for delta in deltas
        ]
        chunks.append(
            {
                "id": "c",
                "object": "chat.completion.chunk",
                "created": 1,
                "model": MODEL,
                "choices": [],
                "usage": usage,
            }
        )
        body = (
            "".join(f"data: {json.dumps(chunk)}\n\n" for chunk in chunks)
            + "data: [DONE]\n\n"
        )
        return httpx.Response(
            200, headers={"content-type": "text/event-stream"}, content=body.encode()
        )
    return httpx.Response(
        200,
        json={
            "id": "c",
            "object": "chat.completion",
            "created": 1,
            "model": MODEL,
            "choices": [
                {
                    "index": 0,
                    "finish_reason": "tool_calls" if outcome == "tool" else "stop",
                    "message": message,
                }
            ],
            "usage": usage,
        },
    )


@pytest.fixture
def setup_route(receiver):
    providers = []

    def setup(route, private=False):
        if route != "legacy" and not MODERN_SDK:
            pytest.skip("The shared OpenAI instrumentor requires openai >= 1.69.0")
        receiver.clear()
        provider = TracerProvider()
        provider.add_span_processor(
            SimpleSpanProcessor(
                OTLPSpanExporter(endpoint=receiver.endpoint, headers={}, timeout=5)
            )
        )
        providers.append(provider)
        config = TraceConfig(
            hide_inputs=private,
            hide_outputs=private,
            hide_llm_invocation_parameters=private,
            hide_error_details=private,
        )
        instruments = {
            "legacy": [FireworksInstrumentor()],
        }
        if MODERN_SDK:
            instruments.update(
                {
                    "shared": [OpenAIInstrumentor()],
                    "legacy_first": [FireworksInstrumentor(), OpenAIInstrumentor()],
                    "shared_first": [OpenAIInstrumentor(), FireworksInstrumentor()],
                }
            )
        for instrumentor in instruments[route]:
            if isinstance(instrumentor, FireworksInstrumentor):
                with pytest.warns(DeprecationWarning, match="OpenAIInstrumentor"):
                    instrumentor.instrument(tracer_provider=provider, config=config)
            else:
                instrumentor.instrument(tracer_provider=provider, config=config)
        return provider

    yield setup
    for instrumentor in (
        [FireworksInstrumentor(), OpenAIInstrumentor()]
        if MODERN_SDK
        else [FireworksInstrumentor()]
    ):
        if instrumentor.is_instrumented_by_opentelemetry:
            instrumentor.uninstrument()
    for provider in providers:
        provider.shutdown()


def invoke(outcome="success", use_async=False, base_url=BASE_URL):
    requests = []

    def handle(request):
        requests.append(request)
        return response(outcome)

    params = {
        "model": MODEL,
        "messages": [{"role": "user", "content": INPUT}],
        "stream": outcome in ("stream", "tool_stream"),
    }
    if outcome in ("tool", "tool_stream"):
        params["tools"] = [
            {
                "type": "function",
                "function": {
                    "name": "lookup",
                    "parameters": {
                        "type": "object",
                        "properties": {"query": {"type": "string"}},
                    },
                },
            }
        ]

    async def run_async():
        async with openai.AsyncOpenAI(
            base_url=base_url,
            api_key="synthetic-not-a-real-key",
            max_retries=0,
            http_client=httpx.AsyncClient(
                transport=httpx.MockTransport(handle), trust_env=False
            ),
        ) as client:
            result = await client.chat.completions.create(**params)
            if params["stream"]:
                return [chunk async for chunk in result]
            return result

    def run_sync():
        with openai.OpenAI(
            base_url=base_url,
            api_key="synthetic-not-a-real-key",
            max_retries=0,
            http_client=httpx.Client(
                transport=httpx.MockTransport(handle), trust_env=False
            ),
        ) as client:
            result = client.chat.completions.create(**params)
            return list(result) if params["stream"] else result

    if outcome == "error":
        with pytest.raises(openai.AuthenticationError, match=ERROR):
            asyncio.run(run_async()) if use_async else run_sync()
        result = None
    else:
        result = asyncio.run(run_async()) if use_async else run_sync()
        if outcome == "success":
            assert result.choices[0].message.content == OUTPUT
        elif outcome == "stream":
            assert (
                "".join(
                    chunk.choices[0].delta.content or ""
                    for chunk in result
                    if chunk.choices
                )
                == OUTPUT
            )
        elif outcome == "tool":
            assert json.loads(
                result.choices[0].message.tool_calls[0].function.arguments
            ) == {"query": ARGUMENT}
    assert len(requests) == 1
    assert requests[0].url.path == "/inference/v1/chat/completions"
    return result


@pytest.mark.parametrize("route", ["shared", "legacy", "legacy_first", "shared_first"])
@pytest.mark.parametrize("use_async", [False, True])
@pytest.mark.parametrize(
    "outcome", ["success", "stream", "tool", "tool_stream", "error"]
)
@pytest.mark.parametrize("private", [False, True])
def test_one_fireworks_span_on_the_wire(
    setup_route, receiver, route, use_async, outcome, private
):
    if outcome in ("tool", "tool_stream") and Version(openai.__version__) < Version(
        "1.1.0"
    ):
        pytest.skip("OpenAI 1.0 does not accept the tools parameter")
    setup_route(route, private)
    invoke(outcome, use_async)
    spans = receiver.spans()
    assert len(spans) == 1, [span["name"] for span in spans]
    span = spans[0]
    attrs = _flatten_attributes(span["attributes"])
    assert attrs["gen_ai.provider.name"] == "fireworks"
    assert attrs["gen_ai.request.model"] == MODEL
    assert attrs["gen_ai.span.kind"] == "LLM"
    assert "parentSpanId" not in span
    assert span["status"]["code"] == (
        "STATUS_CODE_ERROR" if outcome == "error" else "STATUS_CODE_OK"
    )
    wire = json.dumps(spans)
    assert "synthetic-not-a-real-key" not in wire
    if private:
        for secret in (INPUT, OUTPUT, ARGUMENT, ERROR):
            assert secret not in wire
    else:
        assert INPUT in wire
        assert (
            ERROR
            if outcome == "error"
            else ARGUMENT
            if outcome in ("tool", "tool_stream")
            else OUTPUT
        ) in wire
    if not private and route != "legacy" and outcome in ("tool", "tool_stream"):
        prefix = "gen_ai.output.messages.0.message.tool_calls.0.tool_call.function."
        assert attrs[prefix + "name"] == "lookup"
        assert json.loads(attrs[prefix + "arguments"]) == {"query": ARGUMENT}
        assert ARGUMENT in attrs["output.value"]
    if outcome != "error":
        assert int(attrs["gen_ai.usage.input_tokens"]) == 5
        assert int(attrs["gen_ai.usage.output_tokens"]) == 2
        assert int(attrs["gen_ai.usage.total_tokens"]) == 7


@pytest.mark.parametrize(
    "host,provider",
    [
        ("API.FIREWORKS.AI.", "fireworks"),
        ("api.fireworks.ai.customer.example", "openai"),
        ("proxy.api.fireworks.ai", "openai"),
        ("notapi.fireworks.ai", "openai"),
        ("api.openai.com", "openai"),
    ],
)
@pytest.mark.parametrize("route", ["shared", "legacy_first", "shared_first"])
def test_exact_host_attribution(setup_route, receiver, host, provider, route):
    setup_route(route)
    invoke(base_url=f"https://{host}/inference/v1")
    spans = receiver.spans()
    assert len(spans) == 1
    assert (
        _flatten_attributes(spans[0]["attributes"])["gen_ai.provider.name"] == provider
    )


@pytest.mark.parametrize("first", ["legacy_first", "shared_first"])
def test_uninstrument_lifecycles_are_independent(setup_route, receiver, first):
    setup_route(first)
    OpenAIInstrumentor().uninstrument()
    invoke()
    assert len(receiver.spans()) == 1
    assert receiver.spans()[0]["name"] == "Fireworks Chat Completions"
    receiver.clear()
    FireworksInstrumentor().uninstrument()
    invoke()
    assert receiver.spans() == []


@pytest.mark.parametrize("route", ["legacy_first", "shared_first"])
def test_removing_legacy_does_not_disable_shared(setup_route, receiver, route):
    setup_route(route)
    FireworksInstrumentor().uninstrument()
    invoke()
    spans = receiver.spans()
    assert len(spans) == 1
    assert spans[0]["name"] == "ChatCompletion"
    assert (
        _flatten_attributes(spans[0]["attributes"])["gen_ai.provider.name"]
        == "fireworks"
    )


@pytest.mark.parametrize(
    "host,expected",
    [
        ("API.FIREWORKS.AI.", 1),
        ("api.fireworks.ai.customer.example", 0),
        ("proxy.api.fireworks.ai", 0),
        ("notapi.fireworks.ai", 0),
    ],
)
def test_legacy_exact_host(setup_route, receiver, host, expected):
    setup_route("legacy")
    invoke(base_url=f"https://{host}/inference/v1")
    assert len(receiver.spans()) == expected
