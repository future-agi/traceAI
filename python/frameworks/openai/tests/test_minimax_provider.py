"""MiniMax attribution through the shared SDK, with actual localhost OTLP export.

These are shared-route attribution contracts, not a legacy-package retirement
assertion. In particular, streamed tool extraction is covered by the separate
shared accumulator repair; the dedicated package must not be deleted on the
strength of host attribution alone.
"""

import asyncio
import json
import sys
from importlib.metadata import version
from pathlib import Path

import httpx
import openai
import pytest
import requests
from fi_instrumentation import TraceConfig
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from packaging.version import Version
from traceai_openai import OpenAIInstrumentor
from traceai_openai._providers import provider_from_host

# Reuse the repository's loopback OTLP receiver without loading core conftest.py.
sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "tests"))
from harness import Receiver, _flatten_attributes  # noqa: E402

HOSTS = ("api.minimax.io", "api.minimax.cn", "api.minimaxi.com", "api.minimax.chat")
REQUEST_MODEL = "MiniMax-requested-test-model"
RESPONSE_MODEL = "MiniMax-served-test-model"
PROMPT = "synthetic-private-input"
ANSWER = "synthetic-private-answer"
TOOL_ARGUMENTS = '{"query":"synthetic-private-tool-argument"}'
ERROR = "synthetic-private-error"
USAGE = {"prompt_tokens": 5, "completion_tokens": 2, "total_tokens": 7}


@pytest.fixture
def wire():
    with Receiver() as receiver:
        # Do not pick up the viewer's proxy settings or exporter credentials.
        session = requests.Session()
        session.trust_env = False
        exporter = OTLPSpanExporter(endpoint=receiver.endpoint, session=session, headers={})
        provider = TracerProvider()
        provider.add_span_processor(SimpleSpanProcessor(exporter))
        instrumentor = OpenAIInstrumentor()

        def start(config=None):
            instrumentor.instrument(tracer_provider=provider, config=config or TraceConfig())
            return receiver

        try:
            yield start
        finally:
            if instrumentor.is_instrumented_by_opentelemetry:
                instrumentor.uninstrument()
            provider.shutdown()


def completion(tool=False):
    message = {"role": "assistant", "content": None if tool else ANSWER}
    if tool:
        message["tool_calls"] = [{
            "id": "call_test", "type": "function",
            "function": {"name": "lookup", "arguments": TOOL_ARGUMENTS},
        }]
    return {
        "id": "chatcmpl_test", "object": "chat.completion", "created": 1,
        "model": RESPONSE_MODEL,
        "choices": [{"index": 0, "message": message,
                     "finish_reason": "tool_calls" if tool else "stop"}],
        "usage": USAGE,
    }


def stream_response(tool=False, usage=True):
    if tool:
        deltas = [
            {"role": "assistant", "tool_calls": [{
                "index": 0, "id": "call_test", "type": "function",
                "function": {"name": "lookup", "arguments": TOOL_ARGUMENTS[:10]},
            }]},
            {"tool_calls": [{"index": 0, "function": {"arguments": TOOL_ARGUMENTS[10:]}}]},
        ]
    else:
        deltas = [{"role": "assistant", "content": ANSWER[:8]}, {"content": ANSWER[8:]}]
    common = {"id": "chatcmpl_test", "object": "chat.completion.chunk", "created": 1,
              "model": RESPONSE_MODEL}
    chunks = [
        {**common, "choices": [{"index": 0, "delta": delta, "finish_reason": None}]}
        for delta in deltas
    ]
    chunks.append({**common, "choices": [{"index": 0, "delta": {},
                                          "finish_reason": "tool_calls" if tool else "stop"}]})
    if usage:
        chunks.append({**common, "choices": [], "usage": USAGE})
    else:
        # MiniMax's documented stream can include an explicit null usage.
        chunks[-1]["usage"] = None
    body = "".join(f"data: {json.dumps(chunk)}\n\n" for chunk in chunks)
    return httpx.Response(200, headers={"content-type": "text/event-stream"},
                          content=(body + "data: [DONE]\n\n").encode())


def exercise(host, use_async, outcome):
    """Run the real SDK with a synthetic vendor transport; preserve its result."""
    streamed = outcome in ("stream", "tool_stream", "stream_no_usage")
    tool = outcome in ("tool", "tool_stream")

    def handler(request):
        assert request.url.host.rstrip(".").lower() == host.rstrip(".").lower()
        assert request.url.path == "/v1/chat/completions"
        if outcome == "error":
            return httpx.Response(401, json={"error": {
                "message": ERROR, "type": "authentication_error",
            }})
        if streamed:
            return stream_response(tool=tool, usage=outcome != "stream_no_usage")
        return httpx.Response(200, json=completion(tool=tool))

    kwargs = {"model": REQUEST_MODEL, "messages": [{"role": "user", "content": PROMPT}],
              "stream": streamed}
    if tool:
        kwargs["tools"] = [{"type": "function", "function": {
            "name": "lookup", "description": PROMPT,
            "parameters": {"type": "object", "properties": {"query": {"type": "string"}}},
        }}]
    if streamed:
        kwargs["stream_options"] = {"include_usage": True}
    options = {"api_key": "synthetic-test-key", "base_url": f"https://{host}/v1",
               "max_retries": 0}

    async def run_async():
        transport = httpx.AsyncClient(transport=httpx.MockTransport(handler), trust_env=False)
        async with openai.AsyncOpenAI(**options, http_client=transport) as client:
            response = await client.chat.completions.create(**kwargs)
            return [chunk async for chunk in response] if streamed else response

    if use_async:
        result = asyncio.run(run_async())
    else:
        transport = httpx.Client(transport=httpx.MockTransport(handler), trust_env=False)
        with openai.OpenAI(**options, http_client=transport) as client:
            response = client.chat.completions.create(**kwargs)
            result = list(response) if streamed else response

    if streamed:
        deltas = [choice.delta for chunk in result for choice in chunk.choices]
        if tool:
            assert "".join(call.function.arguments or "" for delta in deltas
                           for call in (delta.tool_calls or [])) == TOOL_ARGUMENTS
        else:
            assert "".join(delta.content or "" for delta in deltas) == ANSWER
    elif tool:
        assert result.choices[0].message.tool_calls[0].function.arguments == TOOL_ARGUMENTS
    else:
        assert result.choices[0].message.content == ANSWER
    return result


def only_span(receiver):
    spans = receiver.spans()
    assert len(spans) == 1, [span["name"] for span in spans]
    assert spans[0]["name"] == "ChatCompletion"
    return spans[0], _flatten_attributes(spans[0]["attributes"])


@pytest.mark.parametrize("host", HOSTS)
@pytest.mark.parametrize("use_async", [False, True], ids=["sync", "async"])
@pytest.mark.parametrize("outcome", ["text", "stream", "tool", "tool_stream", "error"])
def test_shared_minimax_provider_on_wire(wire, host, use_async, outcome):
    receiver = wire()
    if outcome == "error":
        with pytest.raises(openai.AuthenticationError, match=ERROR):
            exercise(host, use_async, outcome)
    else:
        exercise(host, use_async, outcome)
    span, attrs = only_span(receiver)
    assert attrs["gen_ai.provider.name"] == "minimax"
    assert attrs["gen_ai.request.model"] == (
        RESPONSE_MODEL if outcome in ("text", "tool") else REQUEST_MODEL
    )
    if outcome == "error":
        assert span["status"]["code"] == "STATUS_CODE_ERROR"
    else:
        assert span["status"]["code"] == "STATUS_CODE_OK"
        assert int(attrs["gen_ai.usage.input_tokens"]) == 5
        assert int(attrs["gen_ai.usage.output_tokens"]) == 2
        assert int(attrs["gen_ai.usage.total_tokens"]) == 7
        if outcome in ("text", "stream"):
            assert attrs["output.value"] == ANSWER
        elif outcome == "tool":
            assert attrs[
                "gen_ai.output.messages.0.message.tool_calls.0.tool_call.function.arguments"
            ] == TOOL_ARGUMENTS


@pytest.mark.parametrize("use_async", [False, True], ids=["sync", "async"])
@pytest.mark.parametrize("outcome", ["text", "stream", "tool", "tool_stream", "error"])
def test_shared_minimax_privacy_on_wire(wire, use_async, outcome):
    if outcome in ("tool", "tool_stream", "error") and Version(
        version("fi-instrumentation-otel")
    ) < Version("1.2.2"):
        pytest.skip("Full tool/error masking is checked on core 1.2.2+ in dedicated CI")
    receiver = wire(TraceConfig(hide_inputs=True, hide_outputs=True,
                                hide_llm_invocation_parameters=True))
    if outcome == "error":
        with pytest.raises(openai.AuthenticationError, match=ERROR):
            exercise(HOSTS[0], use_async, outcome)
    else:
        exercise(HOSTS[0], use_async, outcome)
    span, attrs = only_span(receiver)
    assert attrs["gen_ai.provider.name"] == "minimax"
    serialized = json.dumps(receiver.spans())
    for value in (PROMPT, ANSWER, "synthetic-private-tool-argument", ERROR, "synthetic-test-key"):
        assert value not in serialized
    if outcome == "error":
        assert span["status"]["code"] == "STATUS_CODE_ERROR"
        assert not span["status"].get("message")
    else:
        assert attrs["output.value"] == "__REDACTED__"
        assert int(attrs["gen_ai.usage.total_tokens"]) == 7


@pytest.mark.parametrize("host", HOSTS)
def test_host_normalization(host):
    assert provider_from_host(host.upper() + ".") == "minimax"


@pytest.mark.parametrize("host", [
    "api.minimax.io.customer.example", "notapi.minimax.io", "proxy.api.minimax.io",
    "api.minimax.cn.customer.example", "api.minimaxi.com.customer.example",
    "api.minimax.chat.customer.example", "minimax.io", "api.minimax.invalid",
    "api.openai.com", "llm-proxy.customer.example", "localhost", None,
])
def test_unverified_hosts_are_not_guessed(host):
    assert provider_from_host(host) == "openai"


def test_model_name_does_not_override_provider(wire):
    receiver = wire()
    exercise("llm-proxy.customer.example", False, "text")
    _, attrs = only_span(receiver)
    assert attrs["gen_ai.provider.name"] == "openai"
