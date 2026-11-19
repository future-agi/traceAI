"""Legacy tool correlation IDs, with native SDK controls and no provider traffic."""

import asyncio
from copy import deepcopy
import importlib
import json

import pytest
from fi_instrumentation import TraceConfig
from fi_instrumentation.fi_types import (
    MessageAttributes,
    SpanAttributes,
    ToolCallAttributes,
)
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from traceai_mistralai import MistralAIInstrumentor
from test_sdk import NATIVE, SDK_ROOT, Mistral, httpx


INPUT_ID = "call00001"
OUTPUT_ID = "call00002"
FUNCTION = {"name": "weather", "arguments": '{"city":"test"}'}
MESSAGES = [
    {
        "role": "assistant",
        "tool_calls": [{"id": INPUT_ID, "type": "function", "function": FUNCTION}],
    },
    {"role": "tool", "tool_call_id": INPUT_ID, "content": "sunny"},
]
RESPONSE = {
    "id": "synthetic-completion",
    "object": "chat.completion",
    "created": 1,
    "model": "mistral-test",
    "choices": [
        {
            "index": 0,
            "message": {
                "role": "assistant",
                "tool_calls": [
                    {"id": OUTPUT_ID, "type": "function", "function": FUNCTION}
                ],
            },
            "finish_reason": "tool_calls",
        }
    ],
    "usage": {"prompt_tokens": 2, "completion_tokens": 1, "total_tokens": 3},
}


def _events():
    # Match test_sdk.completion_sse: a message delta followed by a tool call.
    deltas = [
        {"role": "assistant", "content": ""},
        {
            "tool_calls": [
                {
                    "index": 0,
                    "id": OUTPUT_ID,
                    "type": "function",
                    "function": FUNCTION,
                }
            ]
        },
    ]
    for index, delta in enumerate(deltas):
        event = {
            **RESPONSE,
            "object": "chat.completion.chunk",
            "choices": [
                {
                    "index": 0,
                    "delta": delta,
                    "finish_reason": "tool_calls" if index else None,
                }
            ],
            "usage": RESPONSE["usage"] if index else None,
        }
        yield ("data: " + json.dumps(event) + "\n\n").encode()
    yield b"data: [DONE]\n\n"


class _ToolSSE(httpx.SyncByteStream, httpx.AsyncByteStream):
    def __iter__(self):
        yield from _events()

    async def __aiter__(self):
        for event in self:
            yield event


def _complete(messages, streaming, asynchronous):
    observed = []

    def respond(request):
        observed.append(bytes(request.content))
        if streaming:
            return httpx.Response(
                200,
                headers={"content-type": "text/event-stream"},
                stream=_ToolSSE(),
            )
        return httpx.Response(200, json=deepcopy(RESPONSE))

    kwargs = {"model": "mistral-test", "messages": messages}

    async def run():
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(respond)
        ) as transport:
            client = Mistral(api_key="synthetic-test-key", async_client=transport)
            if not streaming:
                return await client.chat.complete_async(**kwargs)
            stream = await client.chat.stream_async(**kwargs)
            try:
                return [chunk async for chunk in stream]
            finally:
                if hasattr(stream, "close"):
                    await stream.close()

    if asynchronous:
        result = asyncio.run(run())
    else:
        with httpx.Client(transport=httpx.MockTransport(respond)) as transport:
            client = Mistral(api_key="synthetic-test-key", client=transport)
            if streaming:
                # mistralai 1.1.0 returns a plain generator; later 1.x returns a
                # context-managed stream. Consume both the same way.
                stream = client.chat.stream(**kwargs)
                try:
                    result = list(stream)
                finally:
                    if hasattr(stream, "close"):
                        stream.close()
            else:
                result = client.chat.complete(**kwargs)
    return observed, result


def _tool_fields(attributes, root, message_index):
    """Read the same semantic fields from native JSON or legacy FI leaves."""
    if NATIVE:
        messages = json.loads(attributes.get(root, "[]"))
        if not messages:
            return {}
        part = next(
            part
            for part in messages[message_index]["parts"]
            if part["type"] == "tool_call"
        )
        return {
            "id": part.get("id"),
            "name": part.get("name"),
            "arguments": part.get("arguments"),
        }
    prefix = f"{root}.{message_index}.{MessageAttributes.MESSAGE_TOOL_CALLS}.0."
    return {
        field: attributes[prefix + leaf]
        for field, leaf in (
            ("id", ToolCallAttributes.TOOL_CALL_ID),
            ("name", ToolCallAttributes.TOOL_CALL_FUNCTION_NAME),
            ("arguments", ToolCallAttributes.TOOL_CALL_FUNCTION_ARGUMENTS_JSON),
        )
        if prefix + leaf in attributes
    }


def _result_id(attributes):
    if NATIVE:
        messages = json.loads(attributes.get(SpanAttributes.GEN_AI_INPUT_MESSAGES, "[]"))
        return messages[1]["parts"][0].get("id") if messages else None
    return attributes.get(
        f"{SpanAttributes.GEN_AI_INPUT_MESSAGES}.1."
        f"{MessageAttributes.MESSAGE_TOOL_CALL_ID}"
    )


@pytest.mark.parametrize("typed", [False, True], ids=["dict", "typed"])
@pytest.mark.parametrize("asynchronous", [False, True], ids=["sync", "async"])
@pytest.mark.parametrize("streaming", [False, True], ids=["complete", "stream"])
@pytest.mark.parametrize(
    "policy",
    [
        "visible",
        "hide_inputs",
        "hide_outputs",
        "hide_input_messages",
        "hide_output_messages",
        "hide_input_text",
        "hide_output_text",
        "hide_both",
    ],
)
def test_tool_id_correlation(typed, asynchronous, streaming, policy):
    messages = deepcopy(MESSAGES)
    if typed:
        models = importlib.import_module(SDK_ROOT + ".models")
        messages = [
            models.AssistantMessage(**messages[0]),
            models.ToolMessage(**messages[1]),
        ]
    original = deepcopy(messages)
    baseline_wire, baseline_result = _complete(messages, streaming, asynchronous)
    assert messages == original
    config = TraceConfig(
        **(
            {"hide_inputs": True, "hide_outputs": True}
            if policy == "hide_both"
            else {} if policy == "visible" else {policy: True}
        )
    )
    provider = TracerProvider()
    exporter = InMemorySpanExporter()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    instrumentor = MistralAIInstrumentor()
    instrumentor.instrument(tracer_provider=provider, config=config)
    try:
        wire, result = _complete(messages, streaming, asynchronous)
        provider.force_flush()
        spans = exporter.get_finished_spans()
    finally:
        instrumentor.uninstrument()
        provider.shutdown()

    # Compare complete serialized requests and SDK results to an uninstrumented
    # call, not just the IDs. Privacy must affect telemetry only.
    assert messages == original
    assert wire == baseline_wire
    assert len(wire) == 1
    body = json.loads(wire[0])
    assert body["messages"][0]["tool_calls"][0]["id"] == INPUT_ID
    assert body["messages"][1]["tool_call_id"] == INPUT_ID
    returned = result if streaming else [result]
    baseline = baseline_result if streaming else [baseline_result]
    assert len(returned) == len(baseline)
    for actual, expected in zip(returned, baseline):
        assert type(actual) is type(expected)
        assert actual.model_dump() == expected.model_dump()
    output_call = (
        result[1].data.choices[0].delta.tool_calls[0]
        if streaming else result.choices[0].message.tool_calls[0]
    )
    assert output_call.id == OUTPUT_ID

    assert len(spans) == 1
    attributes = spans[0].attributes
    hide_input = config.hide_inputs or config.hide_input_messages
    hide_output = config.hide_outputs or config.hide_output_messages
    # One assertion shows all three missing IDs in the RED run. Selective text
    # hiding retains IDs just as it retains function names and arguments.
    assert {
        "input_call": _tool_fields(attributes, SpanAttributes.GEN_AI_INPUT_MESSAGES, 0),
        "input_result_id": _result_id(attributes),
        "output_call": _tool_fields(attributes, SpanAttributes.GEN_AI_OUTPUT_MESSAGES, 0),
    } == {
        "input_call": {} if hide_input else {"id": INPUT_ID, **FUNCTION},
        "input_result_id": None if hide_input else INPUT_ID,
        "output_call": {} if hide_output else {"id": OUTPUT_ID, **FUNCTION},
    }
    encoded = json.dumps(dict(attributes))
    if hide_input:
        assert INPUT_ID not in encoded
    if hide_output:
        assert OUTPUT_ID not in encoded
