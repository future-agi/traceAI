"""Privacy flags must cover redundant and provider-specific attributes on the OTLP wire."""

import json
from types import SimpleNamespace

import pytest
from fi_instrumentation import FITracer, TraceConfig
from opentelemetry.exporter.otlp.proto.common.trace_encoder import encode_spans

from traceai_huggingface._wrappers import (
    _AsyncChatCompletionWrapper,
    _AsyncTextGenerationWrapper,
    _ChatCompletionWrapper,
    _TextGenerationWrapper,
)

SECRET = "synthetic-private-content"


def assert_private(exporter):
    spans = exporter.get_finished_spans()
    assert len(spans) == 1
    assert SECRET.encode() not in encode_spans(spans).SerializeToString()
    return spans[0].attributes


@pytest.mark.parametrize("asynchronous", [False, True])
@pytest.mark.parametrize("flag", ["hide_inputs", "hide_input_text"])
def test_hidden_inputs_omit_tool_definitions_and_choice(telemetry, asynchronous, flag):
    provider, exporter = telemetry
    tracer = FITracer(provider.get_tracer("privacy"), config=TraceConfig(**{flag: True}))
    request = {
        "messages": [{"role": "user", "content": "hello"}],
        "tools": [{"type": "function", "function": {"name": "lookup", "description": SECRET}}],
        "tool_choice": {"type": "function", "function": {"name": SECRET}},
    }
    if asynchronous:
        import asyncio

        async def wrapped(**kwargs):
            return {"choices": []}

        asyncio.run(_AsyncChatCompletionWrapper(tracer=tracer)(
            wrapped, SimpleNamespace(model="offline"), (), request,
        ))
    else:
        _ChatCompletionWrapper(tracer=tracer)(
            lambda **kwargs: {"choices": []}, SimpleNamespace(model="offline"), (), request,
        )
    attrs = assert_private(exporter)
    assert attrs["huggingface.tools_count"] == 1
    assert "huggingface.tools" not in attrs
    assert "huggingface.tool_choice" not in attrs


@pytest.mark.parametrize("asynchronous", [False, True])
@pytest.mark.parametrize("chat", [False, True])
def test_hide_input_text_omits_duplicate_input_value(telemetry, asynchronous, chat):
    provider, exporter = telemetry
    tracer = FITracer(provider.get_tracer("privacy"), config=TraceConfig(hide_input_text=True))
    request = {"messages": [{"role": "user", "content": SECRET}]} if chat else {"prompt": SECRET}
    response = {"choices": []} if chat else {"generated_text": "answer"}
    wrapper = (_AsyncChatCompletionWrapper if chat else _AsyncTextGenerationWrapper) if asynchronous else (
        _ChatCompletionWrapper if chat else _TextGenerationWrapper
    )
    if asynchronous:
        import asyncio

        async def wrapped(**kwargs):
            return response

        asyncio.run(wrapper(tracer=tracer)(wrapped, SimpleNamespace(model="offline"), (), request))
    else:
        wrapper(tracer=tracer)(lambda **kwargs: response, SimpleNamespace(model="offline"), (), request)
    attrs = assert_private(exporter)
    assert "input.value" not in attrs
    assert attrs["gen_ai.input.messages.0.message.content"] == "__REDACTED__"


@pytest.mark.parametrize("asynchronous", [False, True])
@pytest.mark.parametrize("flag", ["hide_inputs", "hide_input_text"])
def test_nonstream_full_text_does_not_reexport_hidden_prompt(telemetry, asynchronous, flag):
    provider, exporter = telemetry
    tracer = FITracer(provider.get_tracer("privacy"), config=TraceConfig(**{flag: True}))
    request = {"prompt": SECRET, "return_full_text": True}
    response = {"generated_text": SECRET + " answer", "details": {"generated_tokens": 1}}
    if asynchronous:
        import asyncio

        async def wrapped(**kwargs):
            return response

        asyncio.run(_AsyncTextGenerationWrapper(tracer=tracer)(
            wrapped, SimpleNamespace(model="offline"), (), request,
        ))
    else:
        _TextGenerationWrapper(tracer=tracer)(
            lambda **kwargs: response, SimpleNamespace(model="offline"), (), request,
        )
    attrs = assert_private(exporter)
    assert attrs["gen_ai.usage.output_tokens"] == 1
    assert "output.value" not in attrs


@pytest.mark.parametrize("asynchronous", [False, True])
@pytest.mark.parametrize("flag", ["hide_output_messages", "hide_output_text"])
def test_nonstream_chat_hides_raw_response(telemetry, asynchronous, flag):
    provider, exporter = telemetry
    tracer = FITracer(provider.get_tracer("privacy"), config=TraceConfig(**{flag: True}))
    response = {"choices": [{"message": {"role": "assistant", "content": SECRET,
        "tool_calls": [{"id": "call", "function": {"name": "lookup", "arguments": '{"query":"' + SECRET + '"}'}}]}}],
        "usage": {"completion_tokens": 2}}
    if asynchronous:
        import asyncio

        async def wrapped(**kwargs):
            return response

        asyncio.run(_AsyncChatCompletionWrapper(tracer=tracer)(
            wrapped, SimpleNamespace(model="offline"), (), {"messages": [{"role": "user", "content": "hello"}]},
        ))
    else:
        _ChatCompletionWrapper(tracer=tracer)(
            lambda **kwargs: response, SimpleNamespace(model="offline"), (),
            {"messages": [{"role": "user", "content": "hello"}]},
        )
    attrs = assert_private(exporter) if flag == "hide_output_messages" else exporter.get_finished_spans()[0].attributes
    assert "output.value" not in attrs
    assert attrs["gen_ai.usage.output_tokens"] == 2
    if flag == "hide_output_text":
        assert SECRET not in json.dumps({k: v for k, v in attrs.items() if "content" in k})
